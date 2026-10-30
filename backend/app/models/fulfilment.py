"""Matches, reservations, pickups and handover events.

The important one is `Reservation`. It is the only entity that moves quantity,
and three database-level mechanisms protect it:

1. A partial unique index means one recipient cannot hold two live reservations
   on the same donation. That is what makes a replayed event a no-op rather
   than a double booking.
2. A BEFORE INSERT trigger refuses the row unless the donation's safety_status
   is ELIGIBLE. The safety gate lives in the database.
3. The donation's quantity CHECK constraint refuses the accompanying UPDATE if
   the arithmetic would oversell.

`HandoverEvent` is the only source of collected and delivered quantities. No
agent node is granted a tool that writes it — see app/agents/permissions.py.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, QuantityType, TimestampMixin, uuid_pk
from app.models.enums import (
    DataSource,
    HandoverKind,
    MatchStatus,
    PickupStatus,
    ReservationStatus,
)


class Match(Base):
    """A ranked recommendation. Carries its score breakdown so the UI can show
    WHY this recipient was proposed, rather than an opaque number."""

    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = uuid_pk()
    donation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("donations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)

    # {"proximity": {"value": 0.82, "weight": 0.30, "raw_m": 2140}, ...}
    # Every component traceable to a database value, never an LLM estimate.
    score_components: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    # Straight-line distance from PostGIS. Road distance, when computed, is on
    # the pickup row with its own data_source.
    distance_meters: Mapped[Decimal | None] = mapped_column(Numeric(10, 1))

    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="match_status", native_enum=True),
        nullable=False,
        default=MatchStatus.PROPOSED,
        index=True,
    )

    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When AWAITING_CONFIRMATION lapses. Drives the timeout branch.
    confirmation_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    decline_reason: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Match rank={self.rank} score={self.score} [{self.status}]>"


class Reservation(Base, TimestampMixin):
    __tablename__ = "reservations"

    id: Mapped[uuid.UUID] = uuid_pk()
    donation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("donations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    match_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("matches.id", ondelete="SET NULL")
    )

    quantity: Mapped[Decimal] = mapped_column(QuantityType, nullable=False)

    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status", native_enum=True),
        nullable=False,
        default=ReservationStatus.HELD,
        index=True,
    )

    # The sweeper releases HELD rows past this instant.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(120))

    # Supplied by the caller; makes a retried reserve request a no-op instead of
    # a second hold. Unique across the table.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    pickups: Mapped[list["Pickup"]] = relationship(back_populates="reservation")

    def __repr__(self) -> str:
        return f"<Reservation {self.quantity} [{self.status}]>"


class Pickup(Base, TimestampMixin):
    """A volunteer's trip for one reservation.

    `handover_code_hash` holds a hash of the code shown to the donor. The
    plaintext is returned exactly once, at scheduling time, and never stored.
    """

    __tablename__ = "pickups"

    id: Mapped[uuid.UUID] = uuid_pk()
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    volunteer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[PickupStatus] = mapped_column(
        Enum(PickupStatus, name="pickup_status", native_enum=True),
        nullable=False,
        default=PickupStatus.SCHEDULED,
        index=True,
    )

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    handover_code_hash: Mapped[str | None] = mapped_column(String(255))
    delivery_code_hash: Mapped[str | None] = mapped_column(String(255))

    # Route estimate. data_source says whether these came from a real routing
    # provider or the simulated one — it is NOT NULL with no default.
    estimated_distance_km: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    estimated_duration_min: Mapped[int | None] = mapped_column(Integer)
    data_source: Mapped[DataSource] = mapped_column(
        Enum(DataSource, name="data_source", native_enum=True, create_type=False),
        nullable=False,
    )

    cancel_reason: Mapped[str | None] = mapped_column(Text)
    reassignment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    reservation: Mapped[Reservation] = relationship(back_populates="pickups")
    handover_events: Mapped[list["HandoverEvent"]] = relationship(
        back_populates="pickup", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Pickup {self.id} [{self.status}]>"


class HandoverEvent(Base):
    """The ONLY source of collected and delivered quantities.

    Requires a verified code and an authenticated actor. Append-only: a
    correction inserts a new row rather than editing one.
    """

    __tablename__ = "handover_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    pickup_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pickups.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[HandoverKind] = mapped_column(
        Enum(HandoverKind, name="handover_kind", native_enum=True), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    # What ACTUALLY moved. May be less than the reserved quantity; that is the
    # partial-collection case and it is a first-class outcome, not an error.
    quantity: Mapped[Decimal] = mapped_column(QuantityType, nullable=False)

    code_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    notes: Mapped[str | None] = mapped_column(Text)

    # Replayed confirmations collapse onto one row.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    data_source: Mapped[DataSource] = mapped_column(
        Enum(DataSource, name="data_source", native_enum=True, create_type=False),
        nullable=False,
    )

    pickup: Mapped[Pickup] = relationship(back_populates="handover_events")

    def __repr__(self) -> str:
        return f"<HandoverEvent {self.kind} qty={self.quantity}>"
