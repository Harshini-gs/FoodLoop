"""Workflow endpoints: start a run, read its real event trail, resume it.

`GET /workflow/{donation_id}/events` is what the frontend's workflow view
renders. It returns rows from `workflow_events` — actual node entries and
exits written by the orchestrator. Nothing here is synthesised to make the
application look intelligent, which is the difference between a workflow
visualization and an animation.
"""

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, get_sessionmaker
from app.core.deps import get_current_user, require_org_membership
from app.core.errors import ValidationFailed
from app.core.logging import get_logger
from app.models import WorkflowRun
from app.schemas.auth import CurrentUser
from app.services.donation import get_donation
from app.workflows.graph import LangGraphUnavailable, run_sequential
from app.workflows.nodes import NodeContext

router = APIRouter(prefix="/workflow", tags=["workflow"])

# ---------------------------------------------------------------------------
# Checkpoint table setup -- read this before changing anything near it.
#
# `AsyncPostgresSaver.setup()` creates LangGraph's checkpoint tables, and in
# langgraph-checkpoint-postgres 3.x it does so with CREATE INDEX CONCURRENTLY.
# A concurrent index build waits for EVERY transaction that was open when it
# started, including transactions it knows nothing about.
#
# That turned this endpoint into a self-deadlock. The handler reads the
# donation first, which opens a read transaction on the request-scoped session
# and holds it for the lifetime of the request. setup() then runs the index
# build on a second connection and waits for that transaction to finish. It
# cannot: the request is blocked on the index build. The symptom was a request
# that hung indefinitely while `pg_stat_activity` showed one connection `idle
# in transaction` and another waiting on `virtualxid`.
#
# Two defences, because either alone is fragile:
#   1. The handler ends its own transaction before entering this path.
#   2. setup() runs at most once per process, so later runs never touch DDL.
# ---------------------------------------------------------------------------

_setup_lock = asyncio.Lock()
_checkpoint_tables_ready = False


async def _ensure_checkpoint_tables(saver: Any) -> None:
    """Create the checkpoint tables once per process, never per request."""
    global _checkpoint_tables_ready
    if _checkpoint_tables_ready:
        return
    async with _setup_lock:
        # Re-check: another request may have completed setup while we waited.
        if _checkpoint_tables_ready:
            return
        await _ensure_checkpoint_tables(saver)
        _checkpoint_tables_ready = True
        log.info("langgraph checkpoint tables ready")
log = get_logger(__name__)


@router.post("/{donation_id}/run")
async def run_workflow(
    donation_id: uuid.UUID,
    raw_description: str | None = Query(default=None),
    engine: str = Query(
        default="auto",
        description="auto | langgraph | sequential — 'sequential' skips checkpointing",
    ),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Run the workflow for a donation until it completes or interrupts.

    With `engine=langgraph` the run is checkpointed after every node and can
    be resumed after a restart. With `engine=sequential` it uses the same
    nodes and routers in plain Python, which is useful for a demo but has no
    durability — a crash loses the run.
    """
    donation = await get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)

    # End this request's read transaction before anything below runs. The read
    # above opened one and the session would hold it until the response is
    # sent, which is long enough to block the checkpointer's concurrent index
    # build forever -- see the note on _ensure_checkpoint_tables. Nothing below
    # uses `session`; the workflow opens its own sessions through `ctx`.
    await session.rollback()

    ctx = NodeContext(get_sessionmaker())

    if engine in ("auto", "langgraph"):
        try:
            return await _run_with_langgraph(
                ctx, donation_id=donation_id, raw_description=raw_description
            )
        except LangGraphUnavailable as exc:
            if engine == "langgraph":
                raise ValidationFailed(
                    f"LangGraph is unavailable: {exc}",
                    details={"hint": "uv sync --extra agents"},
                ) from exc
            log.warning(
                "langgraph unavailable, falling back to the sequential runner",
                extra={"context": {"error": str(exc)}},
            )

    state = await run_sequential(
        ctx, donation_id=donation_id, raw_description=raw_description
    )
    return {
        "engine": "sequential",
        "checkpointed": False,
        "warning": (
            "The sequential runner has no checkpointing: a crash loses this "
            "run. Install the agents extra for the durable path."
        ),
        "state": _public_state(state),
    }


async def _run_with_langgraph(
    ctx: NodeContext, *, donation_id: uuid.UUID, raw_description: str | None
) -> dict[str, Any]:
    from app.core.settings import get_settings
    from app.workflows.graph import build_graph, make_checkpointer
    from app.workflows.nodes import ensure_run
    from app.workflows.state import initial_state

    settings = get_settings()

    async with ctx.sessionmaker() as session:
        run = await ensure_run(session, donation_id=donation_id)

    saver_cm = await make_checkpointer(settings.database_url)
    async with saver_cm as saver:
        await _ensure_checkpoint_tables(saver)
        graph = build_graph(ctx, checkpointer=saver)

        config = {"configurable": {"thread_id": f"donation:{donation_id}"}}
        state = initial_state(
            donation_id=str(donation_id),
            run_id=str(run.id),
            raw_description=raw_description,
        )
        result = await graph.ainvoke(state, config=config)

    return {
        "engine": "langgraph",
        "checkpointed": True,
        "thread_id": f"donation:{donation_id}",
        "state": _public_state(result),
    }


@router.post("/{donation_id}/resume")
async def resume_workflow(
    donation_id: uuid.UUID,
    clarification_answer: str | None = Query(default=None),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Resume an interrupted run from its checkpoint.

    This is the mechanism behind the resume guarantee: the run continues from
    the node it stopped at rather than re-executing the donation, so side
    effects already performed are not repeated.
    """
    donation = await get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)

    # Same reason as in run_workflow: do not hold a read transaction open
    # across the checkpointer.
    await session.rollback()

    from app.core.settings import get_settings
    from app.workflows.graph import build_graph, make_checkpointer

    ctx = NodeContext(get_sessionmaker())
    settings = get_settings()

    try:
        saver_cm = await make_checkpointer(settings.database_url)
    except LangGraphUnavailable as exc:
        raise ValidationFailed(
            f"resume needs LangGraph checkpointing: {exc}",
            details={"hint": "uv sync --extra agents"},
        ) from exc

    async with saver_cm as saver:
        graph = build_graph(ctx, checkpointer=saver)
        config = {"configurable": {"thread_id": f"donation:{donation_id}"}}

        snapshot = await graph.aget_state(config)
        if snapshot is None or not snapshot.next:
            raise ValidationFailed(
                "there is no interrupted run to resume for this donation"
            )

        update: dict[str, Any] = {"needs_human": None}
        if clarification_answer:
            update["clarification_answer"] = clarification_answer

        await graph.aupdate_state(config, update)
        result = await graph.ainvoke(None, config=config)

    return {
        "resumed_from": list(snapshot.next),
        "state": _public_state(result),
    }


@router.get("/{donation_id}/events")
async def workflow_events(
    donation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The real event trail. This is what the workflow UI renders.

    Every row was written by a node on entry or exit. If the UI shows a step,
    it happened.
    """
    donation = await get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)

    run_result = await session.execute(
        select(WorkflowRun).where(WorkflowRun.donation_id == donation_id)
    )
    run = run_result.scalars().first()

    events = await session.execute(
        text(
            """
            SELECT node, phase, from_state, to_state, summary, payload,
                   attempt, duration_ms, created_at
              FROM workflow_events
             WHERE donation_id = :id
             ORDER BY created_at ASC, id ASC
            """
        ),
        {"id": donation_id},
    )
    rows = [dict(r) for r in events.mappings()]

    return {
        "donation_id": str(donation_id),
        "run": (
            {
                "thread_id": run.thread_id,
                "status": run.status.value,
                "current_node": run.current_node,
                "interrupt_reason": run.interrupt_reason,
                "attempts": run.attempts,
                "last_error": run.last_error,
            }
            if run else None
        ),
        "events": rows,
        "event_count": len(rows),
        "note": (
            "Every event above is a row in workflow_events written by the "
            "orchestrator. Nothing here is generated for display."
        ),
    }


@router.get("/branches")
async def branch_catalogue() -> dict[str, Any]:
    """The failure branches, their bounds, and what each demonstrates.

    Published as an endpoint so the control flow can be inspected without
    reading the source.
    """
    from app.workflows.routers import BRANCHES

    return {
        "branches": BRANCHES,
        "note": (
            "Every cycle is bounded by a counter in the workflow state, "
            "compared against a configured cap. No recursion limits are used."
        ),
    }


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    """Trim internal fields before returning state to a client."""
    keep = (
        "donation_id", "safety_outcome", "safety_summary", "candidate_index",
        "reservation_id", "reserved_quantity", "reservation_error",
        "pickup_id", "collected_quantity", "delivered_quantity",
        "impact_summary", "attempts", "needs_human", "terminal_reason",
        "last_error", "node_history", "missing_fields",
        "clarification_question",
    )
    public = {k: state.get(k) for k in keep if k in state}
    candidates = state.get("candidates") or []
    public["candidate_count"] = len(candidates)
    public["top_candidate"] = candidates[0] if candidates else None
    public["excluded"] = state.get("excluded") or []
    return public
