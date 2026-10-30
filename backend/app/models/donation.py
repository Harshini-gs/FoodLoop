"""Donations and the safety subsystem.

The four quantity columns on `donations` carry the central invariant:

    0 < quantity_total
    0 <= quantity_reserved  <= quantity_total
    0 <= quantity_collected <= quantity_reserved
    0 <= quantity_delivered <= quantity_collected

That chain is enforced by a CHECK constraint in the migration, so an
over-allocation cannot be stored even if application logic is wrong. The
constraint is the backstop; the row lock in the reservation repository is the
primary mechanism.

`safety_status` is read by a BEFORE INSERT trigger on `reservations`. Nothing
in Python decides whether the gate is open.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, QuantityType, TimestampMixin, uuid_pk
from app.models.enums import (
    DataSource,
    DonationStatus,
    FoodCategory,
    QuantityUnit,
    SafetyStatus,
)


class Donation(Base, TimestampMixin):
    __tablename__ = "donations"

    id: Mapped[uuid.UUID] = uuid_pk()
    donor_org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # The donor's original words, kept verbatim for audit. The extraction step
    # reads this; it is never overwritten with the parsed version.
    raw_description: Mapped[str | None] = mapped_column(Text)

    # This is the OWNING declaration for the food_category type: exactly one
    # place omits create_type=False, so `create_all()` emits CREATE TYPE once.
    # Every other reference (identity.Organization.accepted_categories, the
    # ruleset filters) passes create_type=False. The migration creates all
    # types explicitly, so this only matters if metadata.create_all() is used.
    food_category: Mapped[FoodCategory] = mapped_column(
        Enum(FoodCategory, name="food_category", native_enum=True),
        nullable=False,
    )
    unit: Mapped[QuantityUnit] = mapped_column(
        Enum(QuantityUnit, name="quantity_unit", native_enum=True), nullable=False
    )

    # --- the invariant chain ---
    quantity_total: Mapped[Decimal] = mapped_column(QuantityType, nullable=False)
    quantity_reserved: Mapped[Decimal] = mapped_column(
        QuantityType, nullable=False, server_default="0"
    )
    quantity_collected: Mapped[Decimal] = mapped_column(
        QuantityType, nullable=False, server_default="0"
    )
    quantity_delivered: Mapped[Decimal] = mapped_column(
        QuantityType, nullable=False, server_default="0"
    )

    # --- safety evidence. Nullable BY DESIGN: missing evidence must be
    # representable, because it is what sends a donation to NEEDS_REVIEW.
    # A NOT NULL here would force the extraction step to invent a value. ---
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_notes: Mapped[str | None] = mapped_column(Text)
    kept_refrigerated: Mapped[bool | None] = mapped_column(Boolean)
    allergen_notes: Mapped[str | None] = mapped_column(Text)

    pickup_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    pickup_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    pickup_address_text: Mapped[str] = mapped_column(Text, nullable=False)
    pickup_location: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326), nullable=False
    )

    status: Mapped[DonationStatus] = mapped_column(
        Enum(DonationStatus, name="donation_status", native_enum=True),
        nullable=False,
        default=DonationStatus.DRAFT,
        index=True,
    )
    safety_status: Mapped[SafetyStatus] = mapped_column(
        Enum(SafetyStatus, name="safety_status", native_enum=True),
        nullable=False,
        default=SafetyStatus.PENDING,
        index=True,
    )

    # Set by the extraction step. Non-empty means the donor still owes us
    # information, and the workflow routes to the clarification branch.
    missing_fields: Mapped[list[str] | None] = mapped_column(JSONB)
    ambiguities: Mapped[list[str] | None] = mapped_column(JSONB)

    # Natural key for duplicate detection: a hash over
    # (donor_org, category, quantity, window). See docs/SPECS.md section 5.
    dedupe_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    data_source: Mapped[DataSource] = mapped_column(
        Enum(DataSource, name="data_source", native_enum=True, create_type=False),
        nullable=False,
    )

    safety_checks: Mapped[list["SafetyCheck"]] = relationship(
        back_populates="donation", cascade="all, delete-orphan"
    )

    @property
    def quantity_remaining(self) -> Decimal:
        """Convenience only. Never trust this across a transaction boundary —
        re-read the row under FOR UPDATE before acting on it."""
        return self.quantity_total - self.quantity_reserved

    def __repr__(self) -> str:
        return f"<Donation {self.title} {self.quantity_total}{self.unit} [{self.status}]>"


class SafetyRuleVersion(Base):
    """An immutable snapshot of a ruleset. Safety checks point at one of these,
    so a decision made under demo_v1 stays explainable after demo_v2 ships."""

    __tablename__ = "safety_rule_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # The engine REFUSES to load a ruleset without this. It is how an
    # illustrative demo policy is kept distinguishable from a verified one.
    policy_status: Mapped[str] = mapped_column(String(120), nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text)

    rules: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    checks: Mapped[list["SafetyCheck"]] = relationship(back_populates="rule_version")

    def __repr__(self) -> str:
        return f"<SafetyRuleVersion {self.version} active={self.is_active}>"


class SafetyCheck(Base):
    """Append-only. A re-evaluation inserts a new row rather than updating one,
    so the full decision history survives."""

    __tablename__ = "safety_checks"

    id: Mapped[uuid.UUID] = uuid_pk()
    donation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("donations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("safety_rule_versions.id", ondelete="RESTRICT"), nullable=False
    )

    outcome: Mapped[SafetyStatus] = mapped_column(
        Enum(SafetyStatus, name="safety_status", native_enum=True, create_type=False),
        nullable=False,
    )

    # One entry per rule: {rule_id, severity, passed, reason, observed, limit}.
    # This is what the UI renders as "why was this rejected".
    per_rule: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    # Rules that could not be evaluated because evidence was absent.
    unevaluated: Mapped[list[str] | None] = mapped_column(JSONB)

    # --- human review, when outcome was NEEDS_REVIEW ---
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
    review_outcome: Mapped[SafetyStatus | None] = mapped_column(
        Enum(SafetyStatus, name="safety_status", native_enum=True, create_type=False)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    donation: Mapped[Donation] = relationship(back_populates="safety_checks")
    rule_version: Mapped[SafetyRuleVersion] = relationship(back_populates="checks")

    def __repr__(self) -> str:
        return f"<SafetyCheck {self.outcome} donation={self.donation_id}>"
