"""The safety gate cannot be bypassed.

This is the single most important test in the project.

It does not test that the service layer checks safety status. It tests that
Postgres itself refuses a reservation against a donation that has not cleared
safety — bypassing every line of Python, which is exactly what a compromised
prompt, a buggy service, or a confused agent would effectively be doing.

When an interviewer asks "how do you stop the LLM approving unsafe food", this
file is the answer.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _setup(session: AsyncSession, safety_status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a donation with the given safety status plus a recipient org."""
    donor_id, recipient_id = uuid.uuid4(), uuid.uuid4()
    user_id, donation_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)

    for org_id, name, kind in (
        (donor_id, "Gate Test Donor", "DONOR"),
        (recipient_id, "Gate Test Recipient", "RECIPIENT"),
    ):
        await session.execute(
            text(
                """
                INSERT INTO organizations
                    (id, name, kind, address_text, location, has_refrigeration,
                     is_verified, is_active, data_source, created_at, updated_at)
                VALUES
                    (:id, :name, CAST(:kind AS org_kind), 'addr',
                     ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                     true, true, true, 'SIMULATED', now(), now())
                """
            ),
            {"id": org_id, "name": name, "kind": kind},
        )

    await session.execute(
        text(
            """
            INSERT INTO users
                (id, email, password_hash, full_name, role, org_id,
                 is_available, is_active, created_at, updated_at)
            VALUES
                (:id, :email, 'x', 'Gate Tester', 'DONOR', :org,
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
                 quantity_total, pickup_window_start, pickup_window_end,
                 pickup_address_text, pickup_location, status, safety_status,
                 data_source, created_at, updated_at)
            VALUES
                (:id, :org, :user, 'Gate test donation', 'COOKED_VEG', 'MEALS', 50.00,
                 :ws, :we, 'addr',
                 ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                 'SAFETY_REVIEW', CAST(:safety AS safety_status),
                 'SIMULATED', now(), now())
            """
        ),
        {
            "id": donation_id, "org": donor_id, "user": user_id,
            "ws": now + timedelta(hours=1), "we": now + timedelta(hours=3),
            "safety": safety_status,
        },
    )
    await session.commit()
    return donation_id, recipient_id


async def _try_reserve(
    session: AsyncSession, donation_id: uuid.UUID, recipient_id: uuid.UUID
) -> None:
    """Insert a reservation with raw SQL — no service layer, no validation."""
    await session.execute(
        text(
            """
            INSERT INTO reservations
                (id, donation_id, recipient_org_id, quantity, status, expires_at,
                 created_at, updated_at)
            VALUES
                (:id, :donation, :recipient, 10.00, 'HELD', :expires, now(), now())
            """
        ),
        {
            "id": uuid.uuid4(), "donation": donation_id, "recipient": recipient_id,
            "expires": datetime.now(UTC) + timedelta(minutes=20),
        },
    )
    await session.commit()


# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blocked_status", ["PENDING", "NEEDS_REVIEW", "REJECTED"])
async def test_reservation_refused_unless_eligible(
    session: AsyncSession, blocked_status: str
) -> None:
    """The gate. Raw SQL, no application code involved, still refused."""
    donation_id, recipient_id = await _setup(session, blocked_status)

    with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
        await _try_reserve(session, donation_id, recipient_id)

    # Assert it failed for the RIGHT reason, not some unrelated constraint.
    assert "safety gate" in str(exc_info.value).lower()
    await session.rollback()

    # And nothing was stored.
    result = await session.execute(
        text("SELECT COUNT(*) FROM reservations WHERE donation_id = :id"),
        {"id": donation_id},
    )
    assert result.scalar_one() == 0


async def test_reservation_allowed_when_eligible(session: AsyncSession) -> None:
    """The gate opens for an ELIGIBLE donation — so the test above is proving
    the gate works, not that reservations are broken in general."""
    donation_id, recipient_id = await _setup(session, "ELIGIBLE")

    await _try_reserve(session, donation_id, recipient_id)

    result = await session.execute(
        text("SELECT COUNT(*) FROM reservations WHERE donation_id = :id"),
        {"id": donation_id},
    )
    assert result.scalar_one() == 1


async def test_gate_blocks_after_status_downgrade(session: AsyncSession) -> None:
    """A donation moved back to NEEDS_REVIEW stops accepting reservations
    immediately. The gate reads live status, not a cached decision."""
    donation_id, recipient_id = await _setup(session, "ELIGIBLE")
    await _try_reserve(session, donation_id, recipient_id)

    await session.execute(
        text("UPDATE donations SET safety_status = 'NEEDS_REVIEW' WHERE id = :id"),
        {"id": donation_id},
    )
    await session.commit()

    second_recipient = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO organizations
                (id, name, kind, address_text, location, has_refrigeration,
                 is_verified, is_active, data_source, created_at, updated_at)
            VALUES
                (:id, 'Second Recipient', 'RECIPIENT', 'addr',
                 ST_SetSRID(ST_MakePoint(77.62, 12.93), 4326)::geography,
                 true, true, true, 'SIMULATED', now(), now())
            """
        ),
        {"id": second_recipient},
    )
    await session.commit()

    with pytest.raises((IntegrityError, DBAPIError)):
        await _try_reserve(session, donation_id, second_recipient)
    await session.rollback()


async def test_handover_requires_verified_code(session: AsyncSession) -> None:
    """code_verified = false is unstorable.

    This is why no agent can mark a donation delivered: a handover row cannot
    exist without a verified code, and no graph node is given the tool that
    verifies one.
    """
    donation_id, recipient_id = await _setup(session, "ELIGIBLE")
    await _try_reserve(session, donation_id, recipient_id)

    reservation = await session.execute(
        text("SELECT id FROM reservations WHERE donation_id = :id"), {"id": donation_id}
    )
    reservation_id = reservation.scalar_one()

    pickup_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO pickups
                (id, reservation_id, status, data_source, reassignment_count,
                 created_at, updated_at)
            VALUES (:id, :res, 'SCHEDULED', 'SIMULATED', 0, now(), now())
            """
        ),
        {"id": pickup_id, "res": reservation_id},
    )
    await session.commit()

    actor = await session.execute(text("SELECT id FROM users LIMIT 1"))
    actor_id = actor.scalar_one()

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text(
                """
                INSERT INTO handover_events
                    (id, pickup_id, kind, actor_user_id, quantity, code_verified,
                     occurred_at, data_source)
                VALUES (:id, :pickup, 'COLLECT', :actor, 10.00, false, now(), 'SIMULATED')
                """
            ),
            {"id": uuid.uuid4(), "pickup": pickup_id, "actor": actor_id},
        )
        await session.commit()
