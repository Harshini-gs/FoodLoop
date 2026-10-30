"""Pickup assignment and verified handover.

The handover path is the structural reason an LLM cannot mark a donation
delivered. Three things have to line up:

  1. a plaintext confirmation code, which exists only on the donor's screen
  2. an authenticated actor, taken from the JWT and never from a request body
  3. a `handover_events` row, whose CHECK constraint refuses `code_verified`
     false

No agent node is granted a tool that reaches this module. The permission
registry in `app/agents/permissions.py` has no entry for it anywhere, and
`test_permissions.py` asserts that stays true.

Volunteer assignment is deliberately simple: nearest available volunteer whose
vehicle can carry the load. The OR-Tools multi-stop optimizer is documented as
future work rather than half-built.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidStateTransition, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.core.security import generate_handover_code, hash_handover_code, verify_handover_code
from app.models import Donation, HandoverEvent, Pickup, Reservation, User
from app.models.enums import (
    DataSource,
    DonationStatus,
    HandoverKind,
    PickupStatus,
    ReservationStatus,
)
from app.providers.routing import get_routing_provider

log = get_logger(__name__)


NEAREST_VOLUNTEER_SQL = text(
    """
    SELECT u.id, u.full_name, u.vehicle_capacity,
           ST_Y(u.home_location::geometry) AS lat,
           ST_X(u.home_location::geometry) AS lon,
           ST_Distance(u.home_location, d.pickup_location) AS distance_m
      FROM users u
      CROSS JOIN (SELECT pickup_location FROM donations WHERE id = :donation_id) d
     WHERE u.role = 'VOLUNTEER'
       AND u.is_active
       AND u.is_available
       AND u.home_location IS NOT NULL
       AND (u.vehicle_capacity IS NULL OR u.vehicle_capacity >= :quantity)
     ORDER BY distance_m
     LIMIT 1
    """
)


async def assign_pickup(
    session: AsyncSession, *, reservation_id: uuid.UUID
) -> tuple[Pickup, str]:
    """Create a pickup and return it with the PLAINTEXT handover code.

    The code is returned exactly once, here. Only its hash is stored, so it
    cannot be recovered from the database — if the donor loses it, the pickup
    has to be reassigned. That is the correct trade.
    """
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise NotFound(f"reservation {reservation_id} does not exist")

    if reservation.status not in (ReservationStatus.HELD, ReservationStatus.CONFIRMED):
        raise InvalidStateTransition(
            f"reservation is {reservation.status.value}; cannot schedule a pickup"
        )

    donation_result = await session.execute(
        select(Donation).where(Donation.id == reservation.donation_id)
    )
    donation = donation_result.scalars().first()
    if donation is None:
        raise NotFound("donation not found for this reservation")

    volunteer_row = await session.execute(
        NEAREST_VOLUNTEER_SQL,
        {"donation_id": donation.id, "quantity": reservation.quantity},
    )
    volunteer = volunteer_row.mappings().first()

    if volunteer is None:
        raise ValidationFailed(
            "no available volunteer has enough vehicle capacity for this pickup",
            details={
                "quantity": str(reservation.quantity),
                "reservation_id": str(reservation_id),
            },
        )

    # Route estimate. The provider stamps its own data_source, so a simulated
    # distance is stored as SIMULATED and the UI can say so.
    recipient_point = await session.execute(
        text(
            "SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon "
            "FROM organizations WHERE id = :id"
        ),
        {"id": reservation.recipient_org_id},
    )
    recipient = recipient_point.mappings().first()

    donor_point = await session.execute(
        text(
            "SELECT ST_Y(pickup_location::geometry) AS lat, "
            "ST_X(pickup_location::geometry) AS lon FROM donations WHERE id = :id"
        ),
        {"id": donation.id},
    )
    donor = donor_point.mappings().first()

    estimate = await get_routing_provider().estimate(
        from_lat=float(donor["lat"]), from_lon=float(donor["lon"]),
        to_lat=float(recipient["lat"]), to_lon=float(recipient["lon"]),
    )

    code = generate_handover_code()

    pickup = Pickup(
        reservation_id=reservation.id,
        volunteer_user_id=volunteer["id"],
        status=PickupStatus.SCHEDULED,
        scheduled_for=donation.pickup_window_start,
        handover_code_hash=hash_handover_code(code),
        estimated_distance_km=Decimal(str(estimate.distance_km)),
        estimated_duration_min=estimate.duration_min,
        data_source=estimate.data_source,
    )
    session.add(pickup)

    donation.status = DonationStatus.IN_TRANSIT
    await session.commit()

    log.info(
        "pickup assigned",
        extra={"context": {
            "pickup_id": str(pickup.id),
            "volunteer": volunteer["full_name"],
            "distance_km": estimate.distance_km,
            "data_source": estimate.data_source.value,
        }},
    )
    # The code is logged nowhere — REDACT_KEYS covers it, but it is simply
    # never passed to the logger in the first place.
    return pickup, code


async def record_handover(
    session: AsyncSession,
    *,
    pickup_id: uuid.UUID,
    kind: HandoverKind,
    code: str,
    quantity: Decimal,
    actor: User,
    notes: str | None = None,
    idempotency_key: str | None = None,
) -> HandoverEvent:
    """Record a real-world handover. The only writer of collected/delivered.

    `quantity` is what ACTUALLY moved and may be less than was reserved. A
    partial handover is a first-class outcome, not an error — 47 of 50 boxes
    because three were dropped is the normal case, and reporting 50 would be
    the bug.
    """
    if idempotency_key:
        existing = await session.execute(
            select(HandoverEvent).where(
                HandoverEvent.idempotency_key == idempotency_key
            )
        )
        found = existing.scalars().first()
        if found is not None:
            return found

    result = await session.execute(select(Pickup).where(Pickup.id == pickup_id))
    pickup = result.scalars().first()
    if pickup is None:
        raise NotFound(f"pickup {pickup_id} does not exist")

    expected_hash = (
        pickup.handover_code_hash if kind == HandoverKind.COLLECT
        else pickup.delivery_code_hash or pickup.handover_code_hash
    )
    if not expected_hash:
        raise InvalidStateTransition("this pickup has no handover code set")

    # The gate. An unverified code produces no row — the CHECK constraint
    # would refuse it anyway, but failing here gives a clean 422.
    if not verify_handover_code(code, expected_hash):
        raise ValidationFailed(
            "handover code is incorrect",
            details={"pickup_id": str(pickup_id)},
        )

    reservation_result = await session.execute(
        select(Reservation).where(Reservation.id == pickup.reservation_id)
    )
    reservation = reservation_result.scalars().first()
    if reservation is None:
        raise NotFound("reservation not found for this pickup")

    if quantity > reservation.quantity:
        raise ValidationFailed(
            f"cannot hand over {quantity}; only {reservation.quantity} was reserved",
            details={"reserved": str(reservation.quantity)},
        )

    if kind == HandoverKind.COLLECT and pickup.status not in (
        PickupStatus.SCHEDULED, PickupStatus.EN_ROUTE
    ):
        raise InvalidStateTransition(
            f"pickup is {pickup.status.value}; cannot record a collection"
        )
    # PARTIAL belongs here as much as COLLECTED. A volunteer who collects less
    # than was reserved -- a full van, a spoiled tray, a miscount -- leaves the
    # pickup PARTIAL, and that is an ordinary outcome, not a failure. Accepting
    # only COLLECTED stranded every partial collection permanently: the food
    # was picked up and no delivery could ever be recorded against it.
    if kind == HandoverKind.DELIVER and pickup.status not in (
        PickupStatus.COLLECTED, PickupStatus.PARTIAL
    ):
        raise InvalidStateTransition(
            f"pickup is {pickup.status.value}; "
            "cannot record a delivery before a collection"
        )

    event = HandoverEvent(
        pickup_id=pickup.id,
        kind=kind,
        actor_user_id=actor.id,
        quantity=quantity,
        code_verified=True,
        occurred_at=datetime.now(UTC),
        notes=notes,
        idempotency_key=idempotency_key,
        data_source=DataSource.REAL,
    )
    session.add(event)

    donation_result = await session.execute(
        select(Donation).where(Donation.id == reservation.donation_id)
    )
    donation = donation_result.scalars().first()

    if kind == HandoverKind.COLLECT:
        donation.quantity_collected = Decimal(donation.quantity_collected) + quantity
        pickup.status = (
            PickupStatus.COLLECTED if quantity == reservation.quantity
            else PickupStatus.PARTIAL
        )
        reservation.status = ReservationStatus.CONSUMED
        # Generate the delivery code now; the recipient will need it.
        delivery_code = generate_handover_code()
        pickup.delivery_code_hash = hash_handover_code(delivery_code)
        event.notes = (notes or "") + f"\n[delivery code: {delivery_code}]"
    else:
        donation.quantity_delivered = Decimal(donation.quantity_delivered) + quantity
        pickup.status = PickupStatus.DELIVERED
        if donation.quantity_delivered >= donation.quantity_reserved:
            donation.status = DonationStatus.COMPLETED
            donation.closed_at = datetime.now(UTC)

    await session.commit()

    log.info(
        "handover recorded",
        extra={"context": {
            "pickup_id": str(pickup_id),
            "kind": kind.value,
            "quantity": str(quantity),
            "actor": str(actor.id),
        }},
    )
    return event


async def get_pickup_for_reservation(
    session: AsyncSession, reservation_id: uuid.UUID
) -> Pickup | None:
    """Return the most recent pickup for a reservation, if any.

    This exists so that a recipient can confirm receipt after closing their
    browser. The plaintext handover codes are deliberately NOT recoverable --
    only their hashes are stored -- but the pickup's identity is not a secret,
    and without it the "confirm receipt" step was reachable only inside the
    single page session that created the pickup.
    """
    result = await session.execute(
        select(Pickup)
        .where(Pickup.reservation_id == reservation_id)
        .order_by(Pickup.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_pickup_for_donation(
    session: AsyncSession, donation_id: uuid.UUID
) -> Pickup | None:
    result = await session.execute(
        text(
            """
            SELECT p.id FROM pickups p
              JOIN reservations r ON r.id = p.reservation_id
             WHERE r.donation_id = :donation_id
             ORDER BY p.created_at DESC LIMIT 1
            """
        ),
        {"donation_id": donation_id},
    )
    row = result.first()
    if row is None:
        return None
    pickup_result = await session.execute(select(Pickup).where(Pickup.id == row[0]))
    return pickup_result.scalars().first()
