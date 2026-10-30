"""Database-level integrity tests.

These are the tests that make the project's central claims checkable rather
than rhetorical. Each one attempts the bad thing with RAW SQL, bypassing every
line of application code, and asserts that Postgres refuses.

If a reviewer asks "how do you know an agent cannot oversell a donation", the
answer is test_cannot_oversell_quantity: not "our service layer checks", but
"the database rejects it even when the service layer is bypassed entirely".
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _make_org(session: AsyncSession, *, kind: str = "RECIPIENT", lat: float = 12.97,
                    lon: float = 77.64, verified: bool = True) -> uuid.UUID:
    org_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO organizations
                (id, name, kind, address_text, location, has_refrigeration,
                 is_verified, is_active, data_source, created_at, updated_at)
            VALUES
                (:id, :name, CAST(:kind AS org_kind), 'test address',
                 ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                 true, :verified, true, 'SIMULATED', now(), now())
            """
        ),
        {"id": org_id, "name": f"Test Org {org_id.hex[:8]}", "kind": kind,
         "lat": lat, "lon": lon, "verified": verified},
    )
    return org_id


async def _make_user(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO users
                (id, email, password_hash, full_name, role, org_id,
                 is_available, is_active, created_at, updated_at)
            VALUES
                (:id, :email, 'x', 'Test User', 'DONOR', :org_id,
                 true, true, now(), now())
            """
        ),
        {"id": user_id, "email": f"{user_id.hex[:12]}@test.example", "org_id": org_id},
    )
    return user_id


async def _make_donation(
    session: AsyncSession,
    *,
    total: str = "50.00",
    reserved: str = "0.00",
    collected: str = "0.00",
    delivered: str = "0.00",
    safety_status: str = "ELIGIBLE",
) -> uuid.UUID:
    org_id = await _make_org(session, kind="DONOR")
    user_id = await _make_user(session, org_id)
    donation_id = uuid.uuid4()
    now = datetime.now(UTC)

    await session.execute(
        text(
            """
            INSERT INTO donations
                (id, donor_org_id, created_by_user_id, title, food_category, unit,
                 quantity_total, quantity_reserved, quantity_collected, quantity_delivered,
                 pickup_window_start, pickup_window_end, pickup_address_text, pickup_location,
                 status, safety_status, data_source, created_at, updated_at)
            VALUES
                (:id, :org, :user, 'Test donation', 'COOKED_VEG', 'MEALS',
                 :total, :reserved, :collected, :delivered,
                 :ws, :we, 'test pickup address',
                 ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                 'ELIGIBLE', CAST(:safety AS safety_status), 'SIMULATED', now(), now())
            """
        ),
        {
            "id": donation_id, "org": org_id, "user": user_id,
            "total": total, "reserved": reserved,
            "collected": collected, "delivered": delivered,
            "ws": now + timedelta(hours=1), "we": now + timedelta(hours=3),
            "safety": safety_status,
        },
    )
    await session.commit()
    return donation_id


# ---------------------------------------------------------------------------
# The quantity invariant chain
# ---------------------------------------------------------------------------

async def test_quantity_total_must_be_positive(session: AsyncSession) -> None:
    """Edge case 2: invalid quantities."""
    with pytest.raises((IntegrityError, DBAPIError)):
        await _make_donation(session, total="0.00")


async def test_quantity_total_cannot_be_negative(session: AsyncSession) -> None:
    with pytest.raises((IntegrityError, DBAPIError)):
        await _make_donation(session, total="-10.00")


async def test_cannot_oversell_quantity(session: AsyncSession) -> None:
    """reserved > total is unstorable. Edge case 9."""
    donation_id = await _make_donation(session, total="50.00", reserved="0.00")

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text("UPDATE donations SET quantity_reserved = 60.00 WHERE id = :id"),
            {"id": donation_id},
        )
        await session.commit()


async def test_cannot_collect_more_than_reserved(session: AsyncSession) -> None:
    donation_id = await _make_donation(session, total="50.00", reserved="30.00")

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text("UPDATE donations SET quantity_collected = 40.00 WHERE id = :id"),
            {"id": donation_id},
        )
        await session.commit()


async def test_cannot_deliver_more_than_collected(session: AsyncSession) -> None:
    """The honesty invariant: you cannot report delivering food you never
    collected."""
    donation_id = await _make_donation(
        session, total="50.00", reserved="50.00", collected="47.00"
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text("UPDATE donations SET quantity_delivered = 50.00 WHERE id = :id"),
            {"id": donation_id},
        )
        await session.commit()


async def test_partial_collection_is_allowed(session: AsyncSession) -> None:
    """Edge case 15: a partial collection is valid, not an error.
    Collected 47 of 50 reserved, delivered all 47."""
    donation_id = await _make_donation(session, total="50.00", reserved="50.00")

    await session.execute(
        text(
            "UPDATE donations SET quantity_collected = 47.00, quantity_delivered = 47.00 "
            "WHERE id = :id"
        ),
        {"id": donation_id},
    )
    await session.commit()

    result = await session.execute(
        text("SELECT quantity_delivered FROM donations WHERE id = :id"), {"id": donation_id}
    )
    assert float(result.scalar_one()) == 47.00


# ---------------------------------------------------------------------------
# Pickup window sanity
# ---------------------------------------------------------------------------

async def test_pickup_window_must_be_ordered(session: AsyncSession) -> None:
    org_id = await _make_org(session, kind="DONOR")
    user_id = await _make_user(session, org_id)
    now = datetime.now(UTC)

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text(
                """
                INSERT INTO donations
                    (id, donor_org_id, created_by_user_id, title, food_category, unit,
                     quantity_total, pickup_window_start, pickup_window_end,
                     pickup_address_text, pickup_location, data_source, created_at, updated_at)
                VALUES
                    (:id, :org, :user, 'Backwards window', 'COOKED_VEG', 'MEALS', 10.00,
                     :ws, :we, 'addr',
                     ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                     'SIMULATED', now(), now())
                """
            ),
            {
                "id": uuid.uuid4(), "org": org_id, "user": user_id,
                # end BEFORE start
                "ws": now + timedelta(hours=3), "we": now + timedelta(hours=1),
            },
        )
        await session.commit()


async def test_data_source_is_required(session: AsyncSession) -> None:
    """A row cannot exist without declaring whether its numbers are real.
    This is the honesty mechanism enforced by the schema rather than the UI."""
    org_id = await _make_org(session, kind="DONOR")
    user_id = await _make_user(session, org_id)
    now = datetime.now(UTC)

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text(
                """
                INSERT INTO donations
                    (id, donor_org_id, created_by_user_id, title, food_category, unit,
                     quantity_total, pickup_window_start, pickup_window_end,
                     pickup_address_text, pickup_location, created_at, updated_at)
                VALUES
                    (:id, :org, :user, 'No provenance', 'COOKED_VEG', 'MEALS', 10.00,
                     :ws, :we, 'addr',
                     ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                     now(), now())
                """
            ),
            {"id": uuid.uuid4(), "org": org_id, "user": user_id,
             "ws": now + timedelta(hours=1), "we": now + timedelta(hours=2)},
        )
        await session.commit()
