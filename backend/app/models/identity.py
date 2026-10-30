"""Users and organizations.

Simplified from the full design for the 7-day scope: the separate
`org_memberships`, `volunteer_profiles` and `recipient_capabilities` tables are
folded in here. One user belongs to at most one organization, and volunteer
details live as nullable columns on the user.

If the project later needs a user in several organizations, split
`org_memberships` back out — the FK on users.org_id becomes the migration
target.
"""

import uuid
from datetime import datetime, time
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, QuantityType, TimestampMixin, uuid_pk
from app.models.enums import DataSource, FoodCategory, OrgKind, UserRole


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[OrgKind] = mapped_column(
        Enum(OrgKind, name="org_kind", native_enum=True), nullable=False
    )

    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(32))

    address_text: Mapped[str] = mapped_column(Text, nullable=False)
    # SRID 4326, geography (not geometry) so ST_DWithin takes METRES.
    # A GIST index on this column is created by the migration.
    location: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326), nullable=False
    )

    # --- recipient capabilities (null/empty for donor orgs) ---
    accepted_categories: Mapped[list[str] | None] = mapped_column(
        ARRAY(Enum(FoodCategory, name="food_category", native_enum=True, create_type=False))
    )
    has_refrigeration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    daily_capacity: Mapped[Decimal | None] = mapped_column(QuantityType)

    # Operating hours as local wall-clock time in DEFAULT_TIMEZONE.
    # Stored as `time` deliberately: "we open at 9am" is not a point in time.
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)

    # --- reliability history, used by the matching score ---
    # Counts, not a stored average, so the Wilson interval can be recomputed.
    offers_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    offers_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pickups_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # No server default. A row cannot exist without saying what it is.
    data_source: Mapped[DataSource] = mapped_column(
        Enum(DataSource, name="data_source", native_enum=True), nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="organization")

    def __repr__(self) -> str:
        return f"<Organization {self.name} ({self.kind})>"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True), nullable=False
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )

    # --- volunteer fields (null for every other role) ---
    vehicle_capacity: Mapped[Decimal | None] = mapped_column(QuantityType)
    vehicle_description: Mapped[str | None] = mapped_column(String(120))
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Where the volunteer starts from, for the nearest-volunteer assignment.
    home_location: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326)
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    organization: Mapped[Organization | None] = relationship(back_populates="users")

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"
