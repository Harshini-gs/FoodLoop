"""The concurrency test.

This is the test an interviewer will ask about, so it is worth reading
carefully before you are asked.

What makes it real: it opens TWO independent database sessions and fires both
reservations with `asyncio.gather`. One session used twice would be serialized
by the client, and the test would prove nothing. Mocking the database would
prove less than nothing.

What it asserts:
    * exactly one reservation succeeded
    * the other raised ReservationConflict (a clean 409, not a crash)
    * quantity_reserved equals quantity_total — nothing was oversold
    * exactly one reservation row exists
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.errors import ReservationConflict, SafetyGateViolation
from app.repositories import reservations as repo

pytestmark = pytest.mark.integration


async def _seed_donation(
    session: AsyncSession, *, total: str = "50.00", reserved: str = "20.00"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A donation with `total - reserved` remaining, plus two recipient orgs."""
    donor_id, recipient_a, recipient_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user_id, donation_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)

    for org_id, name, kind in (
        (donor_id, "Race Donor", "DONOR"),
        (recipient_a, "NGO Annapurna", "RECIPIENT"),
        (recipient_b, "Akshaya Trust", "RECIPIENT"),
    ):
        await session.execute(
            text(
                """
                INSERT INTO organizations
                    (id, name, kind, address_text, location, has_refrigeration,
                     is_verified, is_active, data_source, created_at, updated_at)
                VALUES
                    (:id, :name, CAST(:kind AS org_kind), :addr,
                     ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                     true, true, true, 'SIMULATED', now(), now())
                """
            ),
            {"id": org_id, "name": name, "kind": kind, "addr": "test address"},
        )

    await session.execute(
        text(
            """
            INSERT INTO users
                (id, email, password_hash, full_name, role, org_id,
                 is_available, is_active, created_at, updated_at)
            VALUES (:id, :email, 'x', 'Race Tester', 'DONOR', :org,
                    true, true, now(), now())
            """
        ),
        {"id": user_id, "email": f"{user_id.hex[:12]}@test.example", "org": donor_id},
    )

    await session.execute(
        text(
            """
            INSERT INTO donations
                (id, donor_org_id, created_by_user_id, title, food_category, unit,
                 quantity_total, quantity_reserved, pickup_window_start,
                 pickup_window_end, pickup_address_text, pickup_location,
                 status, safety_status, data_source, created_at, updated_at)
            VALUES
                (:id, :org, :user, 'Race donation', 'COOKED_VEG', 'MEALS',
                 :total, :reserved, :ws, :we, 'addr',
                 ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                 'PARTIALLY_RESERVED', 'ELIGIBLE', 'SIMULATED', now(), now())
            """
        ),
        {
            "id": donation_id, "org": donor_id, "user": user_id,
            "total": total, "reserved": reserved,
            "ws": now + timedelta(hours=1), "we": now + timedelta(hours=3),
        },
    )
    await session.commit()
    return donation_id, recipient_a, recipient_b


async def test_concurrent_reservations_exactly_one_wins(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Two recipients claim the last 30 meals at the same instant."""
    donation_id, recipient_a, recipient_b = await _seed_donation(session)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def claim(org_id: uuid.UUID) -> object:
        # A session of its own — this is what makes the race real.
        async with maker() as own_session:
            try:
                return await repo.reserve(
                    own_session,
                    donation_id=donation_id,
                    recipient_org_id=org_id,
                    quantity=Decimal("30.00"),
                )
            except Exception as exc:
                return exc

    result_a, result_b = await asyncio.gather(claim(recipient_a), claim(recipient_b))

    successes = [r for r in (result_a, result_b) if not isinstance(r, Exception)]
    failures = [r for r in (result_a, result_b) if isinstance(r, Exception)]

    assert len(successes) == 1, (
        f"expected exactly one winner, got {len(successes)}. "
        f"Both succeeding means the row lock is not working."
    )
    assert len(failures) == 1
    assert isinstance(failures[0], ReservationConflict), (
        f"the loser should get a clean ReservationConflict, got "
        f"{type(failures[0]).__name__}: {failures[0]}"
    )

    # Nothing was oversold.
    row = await session.execute(
        text(
            "SELECT quantity_total, quantity_reserved FROM donations WHERE id = :id"
        ),
        {"id": donation_id},
    )
    total, reserved = row.one()
    assert Decimal(reserved) == Decimal(total) == Decimal("50.00")

    # Exactly one row, not two.
    count = await session.execute(
        text("SELECT COUNT(*) FROM reservations WHERE donation_id = :id"),
        {"id": donation_id},
    )
    assert count.scalar_one() == 1


async def test_three_way_race_still_allows_only_what_remains(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """30 remaining, three recipients each want 20. Two can be satisfied at
    most — in fact only one, since 20+20 > 30."""
    donation_id, recipient_a, recipient_b = await _seed_donation(session)

    recipient_c = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO organizations
                (id, name, kind, address_text, location, has_refrigeration,
                 is_verified, is_active, data_source, created_at, updated_at)
            VALUES (:id, 'Third NGO', 'RECIPIENT', 'addr',
                    ST_SetSRID(ST_MakePoint(77.62, 12.93), 4326)::geography,
                    true, true, true, 'SIMULATED', now(), now())
            """
        ),
        {"id": recipient_c},
    )
    await session.commit()

    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def claim(org_id: uuid.UUID) -> object:
        async with maker() as own_session:
            try:
                return await repo.reserve(
                    own_session,
                    donation_id=donation_id,
                    recipient_org_id=org_id,
                    quantity=Decimal("20.00"),
                )
            except Exception as exc:
                return exc

    results = await asyncio.gather(
        claim(recipient_a), claim(recipient_b), claim(recipient_c)
    )
    successes = [r for r in results if not isinstance(r, Exception)]

    assert len(successes) == 1, "20 + 20 exceeds the 30 remaining"

    row = await session.execute(
        text("SELECT quantity_total, quantity_reserved FROM donations WHERE id = :id"),
        {"id": donation_id},
    )
    total, reserved = row.one()
    assert Decimal(reserved) <= Decimal(total)
    assert Decimal(reserved) == Decimal("40.00")   # the pre-existing 20 plus 20


async def test_idempotency_key_prevents_a_second_hold(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """A retried request returns the ORIGINAL reservation.

    This is edge case 13 — duplicate events. Without it, a client retry after
    a timeout silently double-books.
    """
    donation_id, recipient_a, _ = await _seed_donation(session)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    key = f"test-key-{uuid.uuid4()}"

    async with maker() as s1:
        first = await repo.reserve(
            s1, donation_id=donation_id, recipient_org_id=recipient_a,
            quantity=Decimal("10.00"), idempotency_key=key,
        )

    async with maker() as s2:
        second = await repo.reserve(
            s2, donation_id=donation_id, recipient_org_id=recipient_a,
            quantity=Decimal("10.00"), idempotency_key=key,
        )

    assert first.id == second.id, "a replayed request created a second reservation"

    count = await session.execute(
        text("SELECT COUNT(*) FROM reservations WHERE donation_id = :id"),
        {"id": donation_id},
    )
    assert count.scalar_one() == 1

    row = await session.execute(
        text("SELECT quantity_reserved FROM donations WHERE id = :id"),
        {"id": donation_id},
    )
    assert Decimal(row.scalar_one()) == Decimal("30.00")   # 20 + 10, charged once


async def test_reserve_refuses_a_donation_that_has_not_cleared_safety(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """The service layer refuses before the trigger has to."""
    donation_id, recipient_a, _ = await _seed_donation(session)

    await session.execute(
        text("UPDATE donations SET safety_status = 'NEEDS_REVIEW' WHERE id = :id"),
        {"id": donation_id},
    )
    await session.commit()

    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as own_session:
        with pytest.raises(SafetyGateViolation):
            await repo.reserve(
                own_session, donation_id=donation_id,
                recipient_org_id=recipient_a, quantity=Decimal("10.00"),
            )


async def test_release_returns_quantity_to_the_pool(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    donation_id, recipient_a, _ = await _seed_donation(session)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with maker() as s1:
        reservation = await repo.reserve(
            s1, donation_id=donation_id, recipient_org_id=recipient_a,
            quantity=Decimal("30.00"),
        )

    async with maker() as s2:
        await repo.release(s2, reservation_id=reservation.id, reason="recipient declined")

    row = await session.execute(
        text("SELECT quantity_reserved FROM donations WHERE id = :id"),
        {"id": donation_id},
    )
    assert Decimal(row.scalar_one()) == Decimal("20.00")   # back to the original


async def test_release_is_idempotent(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """A timer sweep and a user cancellation can race. Releasing twice must
    not credit the quantity back twice."""
    donation_id, recipient_a, _ = await _seed_donation(session)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with maker() as s1:
        reservation = await repo.reserve(
            s1, donation_id=donation_id, recipient_org_id=recipient_a,
            quantity=Decimal("30.00"),
        )

    async with maker() as s2:
        await repo.release(s2, reservation_id=reservation.id, reason="first")
    async with maker() as s3:
        await repo.release(s3, reservation_id=reservation.id, reason="second")

    row = await session.execute(
        text("SELECT quantity_reserved FROM donations WHERE id = :id"),
        {"id": donation_id},
    )
    assert Decimal(row.scalar_one()) == Decimal("20.00"), (
        "a double release credited the quantity back twice"
    )


async def test_expired_holds_are_swept(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    donation_id, recipient_a, _ = await _seed_donation(session)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with maker() as s1:
        reservation = await repo.reserve(
            s1, donation_id=donation_id, recipient_org_id=recipient_a,
            quantity=Decimal("30.00"),
        )

    # Backdate the expiry rather than sleeping.
    await session.execute(
        text("UPDATE reservations SET expires_at = now() - interval '1 minute' "
             "WHERE id = :id"),
        {"id": reservation.id},
    )
    await session.commit()

    async with maker() as sweeper:
        expired = await repo.expire_due_holds(sweeper)

    assert reservation.id in expired

    row = await session.execute(
        text("SELECT quantity_reserved FROM donations WHERE id = :id"),
        {"id": donation_id},
    )
    assert Decimal(row.scalar_one()) == Decimal("20.00")
