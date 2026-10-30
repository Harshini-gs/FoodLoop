"""Notification provider, used through an outbox.

The pattern matters more than the provider. A notification row is written
inside the SAME transaction as the state change that caused it, so a
notification can never be lost because an email API was down at the wrong
moment. Delivery happens afterwards, from the outbox, retried independently of
the workflow.

Without this, "send the recipient an offer" either happens inside the
transaction (and a provider timeout rolls back the reservation) or outside it
(and a crash loses the notification while keeping the reservation). The outbox
is how you get neither.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models import NotificationRecord
from app.models.enums import NotificationStatus

log = get_logger(__name__)

MAX_DELIVERY_ATTEMPTS = 3


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    provider: str
    error: str | None = None


class NotifyProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def send(
        self, *, channel: str, template: str, payload: dict[str, object]
    ) -> DeliveryResult:
        ...


class ConsoleNotifyProvider(NotifyProvider):
    """Logs the notification. Offline, deterministic, and inspectable.

    Deliberately NOT a silent no-op: the row is written and the delivery is
    logged, so a demo can show the full notification trail.
    """

    name = "console"

    async def send(
        self, *, channel: str, template: str, payload: dict[str, object]
    ) -> DeliveryResult:
        log.info(
            "notification (console provider — not actually sent)",
            extra={"context": {"channel": channel, "template": template, **payload}},
        )
        return DeliveryResult(delivered=True, provider=self.name)


class SMTPNotifyProvider(NotifyProvider):
    """NOT IMPLEMENTED. Raises so the outbox records a real failure rather
    than reporting a delivery that never happened."""

    name = "smtp"

    async def send(
        self, *, channel: str, template: str, payload: dict[str, object]
    ) -> DeliveryResult:
        raise NotImplementedError(
            "the SMTP notification provider is not implemented. "
            "Set NOTIFY_PROVIDER=console for offline development."
        )


_provider: NotifyProvider | None = None


def get_notify_provider() -> NotifyProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        _provider = (
            ConsoleNotifyProvider()
            if settings.notify_provider == "console"
            else SMTPNotifyProvider()
        )
        log.info("notify provider selected", extra={"context": {"provider": _provider.name}})
    return _provider


def reset_notify_provider() -> None:
    global _provider
    _provider = None


# ---------------------------------------------------------------------------
# the outbox
# ---------------------------------------------------------------------------

def enqueue(
    session: AsyncSession,
    *,
    template: str,
    channel: str = "console",
    to_user_id: uuid.UUID | None = None,
    to_org_id: uuid.UUID | None = None,
    donation_id: uuid.UUID | None = None,
    payload: dict[str, object] | None = None,
) -> NotificationRecord:
    """Add a notification to the outbox.

    Does NOT commit — the caller commits, which is the entire point: the row
    lands in the same transaction as the state change that caused it.
    """
    record = NotificationRecord(
        to_user_id=to_user_id,
        to_org_id=to_org_id,
        donation_id=donation_id,
        channel=channel,
        template=template,
        payload=payload or {},
        status=NotificationStatus.QUEUED,
        attempts=0,
    )
    session.add(record)
    return record


async def flush_outbox(session: AsyncSession, *, limit: int = 50) -> tuple[int, int]:
    """Attempt delivery of queued notifications. Returns (sent, failed).

    Claims rows with FOR UPDATE SKIP LOCKED so several workers can run without
    delivering the same notification twice.
    """
    provider = get_notify_provider()

    result = await session.execute(
        select(NotificationRecord)
        .where(NotificationRecord.status == NotificationStatus.QUEUED)
        .order_by(NotificationRecord.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    pending = list(result.scalars().all())

    sent = failed = 0
    for record in pending:
        record.attempts += 1
        record.provider = provider.name
        try:
            outcome = await provider.send(
                channel=record.channel,
                template=record.template,
                payload=dict(record.payload or {}),
            )
            if outcome.delivered:
                record.status = NotificationStatus.SENT
                record.sent_at = datetime.now(UTC)
                sent += 1
            else:
                raise RuntimeError(outcome.error or "provider reported failure")
        except Exception as exc:  # noqa: BLE001 — the outbox records failures
            record.last_error = str(exc)[:2000]
            # Bounded retries. After the cap it is ABANDONED, not silently
            # retried forever — a stuck notification should be visible.
            record.status = (
                NotificationStatus.ABANDONED
                if record.attempts >= MAX_DELIVERY_ATTEMPTS
                else NotificationStatus.QUEUED
            )
            failed += 1

    await session.commit()

    if sent or failed:
        log.info("outbox flushed", extra={"context": {"sent": sent, "failed": failed}})
    return sent, failed


async def pending_count(session: AsyncSession) -> int:
    result = await session.execute(
        text("SELECT COUNT(*) FROM notifications WHERE status = 'QUEUED'")
    )
    return int(result.scalar_one())
