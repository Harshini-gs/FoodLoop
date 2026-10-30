"""Declarative base and shared column mixins.

Two decisions worth knowing:

- Every timestamp is `timestamptz` and stored in UTC. Conversion to
  Asia/Kolkata happens only at the API edge. Pickup windows cross local
  midnight, and comparing naive timestamps is a bug waiting to happen.

- Quantities are NUMERIC(10,2), never float. Partial collections accumulate,
  and binary floats drift.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, MetaData, Numeric, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so Alembic generates stable, readable constraint
# names instead of database-assigned ones. Without this, autogenerate produces
# migrations that cannot drop constraints by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


# Quantity columns share one definition so the precision can never diverge.
QuantityType = Numeric(10, 2)


class TimestampMixin:
    """created_at / updated_at maintained by the database, not the application."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def zero() -> Decimal:
    return Decimal("0.00")
