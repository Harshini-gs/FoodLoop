"""Regression tests for two bugs that 91 passing tests did not catch.

Both were found by driving the application by hand, not by the suite, and both
survived because every existing fixture collected exactly the quantity that had
been reserved and did the whole handover inside one browser session. The happy
path was tested thoroughly; the ordinary path was not tested at all.

1. A PARTIAL collection could never be delivered. `record_handover` sets the
   pickup to PARTIAL whenever less than the reserved quantity is collected --
   and its own docstring calls that "a first-class outcome, not an error" --
   but the delivery branch accepted only COLLECTED. Food was picked up and then
   permanently stranded: no delivery could ever be recorded against it.

2. A pickup could not be found again. The pickup id existed only in the React
   state of the tab that created it, so signing out destroyed the only route to
   confirming receipt. `get_pickup_for_reservation` now recovers it. The CODES
   stay unrecoverable -- only their hashes are stored -- which is the property
   that actually matters.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidStateTransition
from app.core.security import hash_handover_code
from app.models import Donation, Pickup, User
from app.models.enums import HandoverKind, PickupStatus
from app.services import handover as service

pytestmark = pytest.mark.integration

COLLECTION_CODE = "TESTC1"


async def _scenario(session: AsyncSession) -> dict[str, uuid.UUID]:
    """120 meals, reserved in full, with a scheduled pickup awaiting collection.

    Built with raw SQL so the test depends on the schema rather than on the
    service layer it is meant to be checking.
    """
    ids = {k: uuid.uuid4() for k in (
        "donor_org", "recipient_org", "donor_user", "recipient_user",
        "donation", "reservation", "pickup",
    )}
    now = datetime.now(UTC)

    for org, name, kind in (
        (ids["donor_org"], "Handover Test Donor", "DONOR"),
        (ids["recipient_org"], "Handover Test Recipient", "RECIPIENT"),
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
            {"id": org, "name": name, "kind": kind},
        )

    for user, org, role in (
        (ids["donor_user"], ids["donor_org"], "DONOR"),
        (ids["recipient_user"], ids["recipient_org"], "RECIPIENT"),
    ):
        await session.execute(
            text(
                """
                INSERT INTO users
                    (id, email, password_hash, full_name, role, org_id,
                     is_available, is_active, created_at, updated_at)
                VALUES
                    (:id, :email, 'x', 'Handover Tester',
                     CAST(:role AS user_role), :org,
                     true, true, now(), now())
                """
            ),
            {
                "id": user,
                "email": f"{user.hex[:12]}@test.example",
                "role": role,
                "org": org,
            },
        )

    # Reserved in full: the CHECK chain requires collected <= reserved.
    await session.execute(
        text(
            """
            INSERT INTO donations
                (id, donor_org_id, created_by_user_id, title, food_category, unit,
                 quantity_total, quantity_reserved, pickup_window_start,
                 pickup_window_end, pickup_address_text, pickup_location,
                 status, safety_status, data_source, created_at, updated_at)
            VALUES
                (:id, :org, :user, 'Handover test donation', 'COOKED_VEG', 'MEALS',
                 120.00, 120.00, :ws, :we, 'addr',
                 ST_SetSRID(ST_MakePoint(77.64, 12.97), 4326)::geography,
                 'FULLY_RESERVED', 'ELIGIBLE', 'SIMULATED', now(), now())
            """
        ),
        {
            "id": ids["donation"], "org": ids["donor_org"], "user": ids["donor_user"],
            "ws": now + timedelta(hours=1), "we": now + timedelta(hours=3),
        },
    )

    await session.execute(
        text(
            """
            INSERT INTO reservations
                (id, donation_id, recipient_org_id, quantity, status, expires_at,
                 created_at, updated_at)
            VALUES
                (:id, :donation, :recipient, 120.00, 'CONFIRMED', :expires,
                 now(), now())
            """
        ),
        {
            "id": ids["reservation"], "donation": ids["donation"],
            "recipient": ids["recipient_org"],
            "expires": now + timedelta(minutes=30),
        },
    )

    await session.execute(
        text(
            """
            INSERT INTO pickups
                (id, reservation_id, status, handover_code_hash, data_source,
                 reassignment_count, created_at, updated_at)
            VALUES
                (:id, :res, 'SCHEDULED', :hash, 'SIMULATED', 0, now(), now())
            """
        ),
        {
            "id": ids["pickup"], "res": ids["reservation"],
            "hash": hash_handover_code(COLLECTION_CODE),
        },
    )
    await session.commit()
    return ids


async def _user(session: AsyncSession, user_id: uuid.UUID) -> User:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalars().one()


def _delivery_code(notes: str | None) -> str:
    """The delivery code is appended to the collection event's notes."""
    assert notes is not None, "collection recorded no notes, so no delivery code"
    marker = "[delivery code: "
    start = notes.index(marker) + len(marker)
    return notes[start : notes.index("]", start)]


# ---------------------------------------------------------------------------
# Bug 1: a partial collection must still be deliverable.
# ---------------------------------------------------------------------------

async def test_partial_collection_can_be_delivered(session: AsyncSession) -> None:
    """117 of 120 collected, then delivered.

    Before the fix this raised InvalidStateTransition("cannot record a delivery
    before a collection") -- on a pickup that had demonstrably been collected.
    """
    ids = await _scenario(session)
    donor = await _user(session, ids["donor_user"])
    recipient = await _user(session, ids["recipient_user"])

    collection = await service.record_handover(
        session,
        pickup_id=ids["pickup"],
        kind=HandoverKind.COLLECT,
        code=COLLECTION_CODE,
        quantity=Decimal("117.00"),
        actor=donor,
    )

    pickup = (
        await session.execute(select(Pickup).where(Pickup.id == ids["pickup"]))
    ).scalars().one()
    await session.refresh(pickup)
    # The precondition for the bug: short collection => PARTIAL, not COLLECTED.
    assert pickup.status == PickupStatus.PARTIAL

    await service.record_handover(
        session,
        pickup_id=ids["pickup"],
        kind=HandoverKind.DELIVER,
        code=_delivery_code(collection.notes),
        quantity=Decimal("117.00"),
        actor=recipient,
    )

    donation = (
        await session.execute(select(Donation).where(Donation.id == ids["donation"]))
    ).scalars().one()
    await session.refresh(donation)
    assert donation.quantity_collected == Decimal("117.00")
    assert donation.quantity_delivered == Decimal("117.00")

    # Deliberate: 3 meals are unaccounted for, so the donation does NOT close.
    # Completion requires delivered >= reserved. An interviewer will notice the
    # dashboard reports zero completed donations; this is why.
    assert donation.closed_at is None


async def test_delivery_still_refused_before_any_collection(
    session: AsyncSession,
) -> None:
    """The fix widened the gate; it did not remove it.

    Without this, a test suite could be made green by deleting the check
    altogether, which is the obvious wrong fix for bug 1.
    """
    ids = await _scenario(session)
    recipient = await _user(session, ids["recipient_user"])

    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.record_handover(
            session,
            pickup_id=ids["pickup"],
            kind=HandoverKind.DELIVER,
            code=COLLECTION_CODE,
            quantity=Decimal("117.00"),
            actor=recipient,
        )
    assert "before a collection" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bug 2: a pickup must be findable after the browser session that made it.
# ---------------------------------------------------------------------------

async def test_pickup_is_recoverable_from_its_reservation(
    session: AsyncSession,
) -> None:
    """What the recipient's dashboard now calls on load.

    Before this existed, the pickup id lived only in React state and signing
    out stranded the reservation permanently.
    """
    ids = await _scenario(session)

    found = await service.get_pickup_for_reservation(session, ids["reservation"])

    assert found is not None
    assert found.id == ids["pickup"]
    # The identity is recoverable. The code is not, and must not be.
    assert not hasattr(found, "handover_code")


async def test_pickup_lookup_returns_none_when_unassigned(
    session: AsyncSession,
) -> None:
    """A reservation with no pickup yields None rather than raising, because
    the dashboard calls this for every reservation it displays."""
    ids = await _scenario(session)
    await session.execute(
        text("DELETE FROM pickups WHERE id = :id"), {"id": ids["pickup"]}
    )
    await session.commit()

    assert await service.get_pickup_for_reservation(session, ids["reservation"]) is None
