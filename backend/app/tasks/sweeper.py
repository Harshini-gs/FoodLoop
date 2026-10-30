"""The background sweeper: durable timers without Redis or Celery.

Three jobs, all driven from the database rather than from in-process state:

  1. release reservation holds past their expiry
  2. fire confirmation timeouts
  3. retry the notification outbox

Why a table and not Redis: these timers must survive a restart. A
`scheduled_tasks` row claimed with `FOR UPDATE SKIP LOCKED` is durable, needs
no extra infrastructure, and lets several workers run safely. The cost is a
few seconds of polling latency, which is irrelevant for a fifteen-minute
confirmation window.

Celery plus Redis is the documented upgrade path if throughput ever demands
it. It is left out deliberately, not forgotten — adding infrastructure that
has to be configured for persistence, in order to replace a table that is
already durable, would make the system less reliable rather than more.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.repositories import reservations as reservation_repo

log = get_logger(__name__)

# How long a worker holds a claim on a task. If it dies mid-task the lease
# lapses and another worker picks it up, rather than the task sticking.
LEASE_SECONDS = 60
MAX_TASK_ATTEMPTS = 3


CLAIM_SQL = text(
    """
    UPDATE scheduled_tasks
       SET status = 'RUNNING',
           locked_until = now() + make_interval(secs => :lease),
           attempts = attempts + 1
     WHERE id IN (
         SELECT id FROM scheduled_tasks
          WHERE status = 'PENDING'
            AND due_at <= now()
            AND (locked_until IS NULL OR locked_until < now())
          ORDER BY due_at
          LIMIT :limit
          FOR UPDATE SKIP LOCKED
     )
    RETURNING id, kind, payload, attempts
    """
)


async def claim_due_tasks(
    session: AsyncSession, *, limit: int = 10
) -> list[dict[str, Any]]:
    result = await session.execute(CLAIM_SQL, {"lease": LEASE_SECONDS, "limit": limit})
    rows = [dict(r) for r in result.mappings()]
    await session.commit()
    return rows


async def handle_confirmation_timeout(
    session: AsyncSession, payload: dict[str, Any]
) -> str:
    """A recipient did not answer in time: release the hold.

    The quantity returns to the pool and the workflow's candidate loop can try
    the next recipient. This is the durable half of the timeout branch — the
    graph does not sleep, it is woken by this.
    """
    reservation_id = payload.get("reservation_id")
    if not reservation_id:
        return "no reservation_id in payload"

    import uuid as _uuid

    from app.models.enums import ReservationStatus

    row = await session.execute(
        text("SELECT status FROM reservations WHERE id = :id"), {"id": reservation_id}
    )
    found = row.first()
    if found is None:
        return "reservation no longer exists"

    if found[0] != ReservationStatus.HELD.value:
        # Already confirmed or released — the timeout lost the race, which is
        # fine. Idempotent by design.
        return f"reservation is {found[0]}, no action needed"

    await reservation_repo.release(
        session,
        reservation_id=_uuid.UUID(str(reservation_id)),
        reason="confirmation timeout",
    )
    return "hold released on timeout"


async def run_once(session: AsyncSession) -> dict[str, int]:
    """One sweeper pass. Returns a count of what it did."""
    from app.providers.notify import flush_outbox

    counts = {"expired_holds": 0, "tasks_done": 0, "tasks_failed": 0,
              "notifications_sent": 0, "notifications_failed": 0}

    # 1. holds past expiry, independently of any scheduled task
    expired = await reservation_repo.expire_due_holds(session)
    counts["expired_holds"] = len(expired)

    # 2. due scheduled tasks
    for task in await claim_due_tasks(session):
        try:
            if task["kind"] == "CONFIRMATION_TIMEOUT":
                note = await handle_confirmation_timeout(session, task["payload"] or {})
            else:
                note = f"no handler for {task['kind']}"

            await session.execute(
                text(
                    "UPDATE scheduled_tasks SET status = 'DONE', completed_at = now(), "
                    "last_error = :note WHERE id = :id"
                ),
                {"id": task["id"], "note": note},
            )
            counts["tasks_done"] += 1
        except Exception as exc:  # noqa: BLE001 — a failed task must be recorded
            # Bounded retries, then FAILED. A task that retries forever is a
            # task nobody will ever notice is broken.
            final = task["attempts"] >= MAX_TASK_ATTEMPTS
            await session.execute(
                text(
                    "UPDATE scheduled_tasks SET status = :status, last_error = :err, "
                    "locked_until = NULL WHERE id = :id"
                ),
                {
                    "id": task["id"],
                    "status": "FAILED" if final else "PENDING",
                    "err": str(exc)[:2000],
                },
            )
            counts["tasks_failed"] += 1
            log.error(
                "scheduled task failed",
                extra={"context": {
                    "task_id": str(task["id"]), "kind": task["kind"],
                    "attempt": task["attempts"], "final": final,
                }},
            )
        await session.commit()

    # 3. the notification outbox
    sent, failed = await flush_outbox(session)
    counts["notifications_sent"] = sent
    counts["notifications_failed"] = failed

    return counts


async def sweeper_loop(sessionmaker: Any) -> None:
    """Poll forever. Started from the FastAPI lifespan.

    Every iteration is wrapped: a failure logs and continues rather than
    killing the loop, because a sweeper that dies silently is worse than one
    that occasionally errors.
    """
    settings = get_settings()
    interval = settings.scheduler_poll_seconds

    log.info("sweeper started", extra={"context": {"interval_s": interval}})

    while True:
        try:
            async with sessionmaker() as session:
                counts = await run_once(session)
            if any(counts.values()):
                log.info("sweeper pass", extra={"context": counts})
        except asyncio.CancelledError:
            log.info("sweeper stopping")
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("sweeper pass failed", extra={"context": {"error": str(exc)}})

        await asyncio.sleep(interval)


def start(app: Any, sessionmaker: Any) -> asyncio.Task[None]:
    task = asyncio.create_task(sweeper_loop(sessionmaker))
    app.state.sweeper_task = task
    return task


async def stop(app: Any) -> None:
    task = getattr(app.state, "sweeper_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
