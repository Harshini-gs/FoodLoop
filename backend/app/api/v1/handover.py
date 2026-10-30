"""Pickup assignment and handover confirmation.

The handover endpoints take the actor from the JWT, never from the request
body. A client cannot claim "this was confirmed by user X" — it can only
confirm as whoever it is authenticated as. That, plus the confirmation code,
plus the CHECK constraint on `handover_events.code_verified`, is why no agent
can mark a donation delivered.
"""

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user, require_active_user, require_org_membership
from app.core.errors import Forbidden, ValidationFailed
from app.models import Donation, Pickup, Reservation, User
from app.models.enums import HandoverKind, UserRole
from app.schemas.auth import CurrentUser
from app.schemas.matching import HandoverOut, HandoverRequest, PickupOut, PickupScheduled
from app.services import handover as service

router = APIRouter(prefix="/handover", tags=["handover"])


@router.post(
    "/reservations/{reservation_id}/assign",
    response_model=PickupScheduled,
    status_code=status.HTTP_201_CREATED,
)
async def assign(
    reservation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Assign the nearest available volunteer with enough vehicle capacity.

    **The response contains the plaintext handover code, and it is the only
    time it is ever returned.** Only a hash is stored, so if the donor loses
    it the pickup must be reassigned.

    Returns 422 with a readable reason when no volunteer fits — infeasibility
    is explained, not swallowed.
    """
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise ValidationFailed("reservation not found")

    donation_result = await session.execute(
        select(Donation).where(Donation.id == reservation.donation_id)
    )
    donation = donation_result.scalars().first()

    # Either side of the handover may schedule it.
    if current.role != UserRole.ADMIN and current.org_id not in (
        donation.donor_org_id, reservation.recipient_org_id
    ):
        raise Forbidden("you are not a party to this reservation")

    pickup, code = await service.assign_pickup(session, reservation_id=reservation_id)

    return {
        "id": pickup.id,
        "reservation_id": pickup.reservation_id,
        "volunteer_user_id": pickup.volunteer_user_id,
        "status": pickup.status,
        "scheduled_for": pickup.scheduled_for,
        "estimated_distance_km": pickup.estimated_distance_km,
        "estimated_duration_min": pickup.estimated_duration_min,
        "data_source": pickup.data_source,
        "reassignment_count": pickup.reassignment_count,
        "handover_code": code,
    }


@router.post("/pickups/{pickup_id}/collect", response_model=HandoverOut)
async def collect(
    pickup_id: uuid.UUID,
    payload: HandoverRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_active_user),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Record a collection. The DONOR confirms, reading the code aloud.

    `quantity` is what ACTUALLY left the kitchen. If three boxes were dropped,
    record 47 — the system is built to carry that honestly, and the impact
    report will show 47 rather than the 50 that were offered.
    """
    return await service.record_handover(
        session,
        pickup_id=pickup_id,
        kind=HandoverKind.COLLECT,
        code=payload.code,
        quantity=payload.quantity,
        actor=user,                 # from the JWT, never from the body
        notes=payload.notes,
        idempotency_key=idempotency_key or payload.idempotency_key,
    )


@router.post("/pickups/{pickup_id}/deliver", response_model=HandoverOut)
async def deliver(
    pickup_id: uuid.UUID,
    payload: HandoverRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_active_user),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Record a delivery. The RECIPIENT confirms what actually arrived."""
    return await service.record_handover(
        session,
        pickup_id=pickup_id,
        kind=HandoverKind.DELIVER,
        code=payload.code,
        quantity=payload.quantity,
        actor=user,
        notes=payload.notes,
        idempotency_key=idempotency_key or payload.idempotency_key,
    )


@router.get("/donations/{donation_id}/pickup", response_model=PickupOut | None)
async def pickup_for_donation(
    donation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Pickup | None:
    from app.services.donation import get_donation

    donation = await get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)
    return await service.get_pickup_for_donation(session, donation_id)


@router.get("/reservations/{reservation_id}/pickup", response_model=PickupOut | None)
async def pickup_for_reservation(
    reservation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Pickup | None:
    """Look up the live pickup for a reservation.

    Without this, the recipient's "confirm receipt" step existed only in the
    browser tab that had just assigned the volunteer: the pickup id lived in
    React state and nothing could recover it. Closing the tab stranded the
    reservation. Authorisation mirrors `assign` -- either party to the
    handover may read it, nobody else.

    `PickupOut` carries no handover code, so this leaks nothing: the codes are
    stored only as hashes and remain unrecoverable by design.
    """
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise ValidationFailed("reservation not found")

    donation_result = await session.execute(
        select(Donation).where(Donation.id == reservation.donation_id)
    )
    donation = donation_result.scalars().first()
    if donation is None:
        raise ValidationFailed("donation not found")

    if current.role != UserRole.ADMIN and current.org_id not in (
        donation.donor_org_id,
        reservation.recipient_org_id,
    ):
        raise Forbidden("you are not a party to this reservation")

    return await service.get_pickup_for_reservation(session, reservation_id)
