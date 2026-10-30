"""Workflow bookkeeping, notifications and durable timers.

`WorkflowEvent` is what the frontend's workflow visualization renders. Every
node writes an ENTER and an EXIT row. That is the whole reason the UI can be
honest: it displays rows that exist, rather than a hard-coded animation.

`ScheduledTask` replaces Redis + Celery. A row with `due_at` and `locked_until`
swept with `FOR UPDATE SKIP LOCKED` gives durable timers that survive a restart
with no extra infrastructure. The trade-off is poll latency of a few seconds,
which is irrelevant for a 15-minute confirmation window.

`NotificationRecord` is an outbox: the row is written inside the same
transaction as the state change that caused it, so a notification can never be
lost by a provider failing. Delivery is retried by the sweeper, independently
of the workflow.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.enums import (
    EventPhase,
    NotificationStatus,
    TaskKind,
    TaskStatus,
    WorkflowRunStatus,
)


class WorkflowRun(Base, TimestampMixin):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    # LangGraph's thread id. One per donation: "donation:<uuid>".
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    donation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("donations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, name="workflow_run_status", native_enum=True),
        nullable=False,
        default=WorkflowRunStatus.RUNNING,
        index=True,
    )
    current_node: Mapped[str | None] = mapped_column(String(64))

    # {"clarification": 1, "candidate": 2, "node:plan_route": 1}
    # The loop bounds live here. Every cycle in the graph increments a counter
    # and compares it against its cap in settings — no recursion limits.
    attempts: Mapped[dict[str, int] | None] = mapped_column(JSONB)

    # Why the run is parked, when status is WAITING_HUMAN.
    interrupt_reason: Mapped[str | None] = mapped_column(String(120))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["WorkflowEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<WorkflowRun {self.thread_id} [{self.status}]>"


class WorkflowEvent(Base):
    """Append-only node trace. This is the source for the workflow UI."""

    __tablename__ = "workflow_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    donation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("donations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    node: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[EventPhase] = mapped_column(
        Enum(EventPhase, name="event_phase", native_enum=True), nullable=False
    )

    from_state: Mapped[str | None] = mapped_column(String(48))
    to_state: Mapped[str | None] = mapped_column(String(48))

    # Human-readable one-liner for the UI timeline.
    summary: Mapped[str | None] = mapped_column(Text)
    # Structured detail: chosen recipient, score breakdown, rule failures.
    # Must never contain a secret — logging.REDACT_KEYS applies here too.
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()", index=True
    )

    run: Mapped[WorkflowRun] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<WorkflowEvent {self.node}/{self.phase}>"


class NotificationRecord(Base):
    """Outbox row. Written transactionally; delivered by the sweeper."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    to_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL")
    )
    donation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("donations.id", ondelete="CASCADE"), index=True
    )

    channel: Mapped[str] = mapped_column(String(16), nullable=False)   # email | sms | console
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    # Rendering context. No contact details beyond what the template needs.
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status", native_enum=True),
        nullable=False,
        default=NotificationStatus.QUEUED,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Notification {self.template} [{self.status}]>"


class ScheduledTask(Base):
    """Durable timer. Swept with FOR UPDATE SKIP LOCKED.

    `locked_until` is a lease, not a lock: if a worker dies mid-task the lease
    lapses and another worker picks it up, rather than the task being stuck.
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, name="task_kind", native_enum=True), nullable=False, index=True
    )

    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # {"reservation_id": "...", "match_id": "..."} — whatever the handler needs.
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", native_enum=True),
        nullable=False,
        default=TaskStatus.PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    # Stops a duplicate timer being scheduled for the same cause.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ScheduledTask {self.kind} due={self.due_at} [{self.status}]>"
