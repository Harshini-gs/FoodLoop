"""Matching and reservation endpoints.

`POST /matching/reserve` is where the race condition lives. It returns
**409 Conflict** when another recipient won the quantity, which is a normal
outcome rather than an error — the frontend shows "someone else just claimed
this" and refreshes.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user, require_active_user, require_org_membership, require_roles
from app.core.errors import ValidationFailed
from app.models import Reservation, User
from app.models.enums import UserRole
from app.repositories import reservations as reservation_repo
from app.schemas.auth import CurrentUser
from app.schemas.matching import MatchingResult, ReservationOut, ReserveRequest
from app.services import donation as donation_service
from app.services import matching as matching_service

router = APIRouter(prefix="/matching", tags=["matching"])


@router.get("/{donation_id}/candidates", response_model=MatchingResult)
async def candidates(
    donation_id: uuid.UUID,
    persist: bool = Query(
        default=False, description="Also write the ranking to the matches table"
    ),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Rank eligible recipients, with the score breakdown for each.

    `excluded` is returned too, so a donor can see why a nearby NGO was not
    offered the food. Silence there looks like a bug to a user.
    """
    donation = await donation_service.get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)

    needs_cold = donation.food_category.value in ("DAIRY", "COOKED_NONVEG")

    kept, excluded, radius, widenings = await matching_service.find_candidates_widening(
        session,
        donation_id=donation_id,
        food_category=donation.food_category.value,
        quantity=donation.quantity_total - donation.quantity_reserved,
        window_start=donation.pickup_window_start,
        window_end=donation.pickup_window_end,
        needs_refrigeration=needs_cold,
    )

    if persist and kept:
        await matching_service.persist_matches(
            session, donation_id=donation_id, candidates=kept
        )

    return {
        "donation_id": donation_id,
        "search_radius_meters": radius,
        "widenings_applied": widenings,
        "candidates": [
            {
                "recipient_org_id": c.recipient_org_id,
                "recipient_name": c.recipient_name,
                "rank": i,
                "score": c.score,
                "components": c.components,
                "distance_meters": c.distance_meters,
                "allocatable_quantity": c.allocatable_quantity,
                "eligibility_notes": [],
            }
            for i, c in enumerate(kept, start=1)
        ],
        "excluded": [
            {
                "recipient_org_id": e.recipient_org_id,
                "recipient_name": e.recipient_name,
                "reason": e.reason,
            }
            for e in excluded
        ],
        "evaluated_at": datetime.now(UTC),
    }


@router.post(
    "/reserve",
    response_model=ReservationOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Another recipient claimed this quantity first"}},
    dependencies=[Depends(require_roles(UserRole.RECIPIENT, UserRole.ADMIN))],
)
async def reserve(
    payload: ReserveRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_active_user),
    session: AsyncSession = Depends(get_session),
) -> Reservation:
    """Claim quantity from a donation.

    Returns 409 if another recipient won the race, or if the donation has not
    cleared safety. Both are expected outcomes, not server errors.

    Pass an `Idempotency-Key` header to make a retry safe: the same key
    returns the original reservation rather than creating a second hold.
    """
    if user.org_id is None:
        raise ValidationFailed("your account is not linked to a recipient organization")

    return await reservation_repo.reserve(
        session,
        donation_id=payload.donation_id,
        recipient_org_id=user.org_id,
        quantity=payload.quantity,
        idempotency_key=idempotency_key or payload.idempotency_key,
    )


@router.post("/reservations/{reservation_id}/confirm", response_model=ReservationOut)
async def confirm(
    reservation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Reservation:
    """Recipient accepts: the hold stops expiring."""
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise ValidationFailed("reservation not found")
    require_org_membership(reservation.recipient_org_id, current)

    return await reservation_repo.confirm(session, reservation_id=reservation_id)


@router.post("/reservations/{reservation_id}/decline", response_model=ReservationOut)
async def decline(
    reservation_id: uuid.UUID,
    reason: str = Query(default="declined by recipient", max_length=120),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Reservation:
    """Recipient declines: the quantity returns to the pool immediately."""
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise ValidationFailed("reservation not found")
    require_org_membership(reservation.recipient_org_id, current)

    return await reservation_repo.release(
        session, reservation_id=reservation_id, reason=reason
    )


@router.get("/reservations/mine", response_model=list[ReservationOut])
async def my_reservations(
    user: User = Depends(require_active_user),
    session: AsyncSession = Depends(get_session),
) -> list[Reservation]:
    if user.org_id is None:
        return []
    result = await session.execute(
        select(Reservation)
        .where(Reservation.recipient_org_id == user.org_id)
        .order_by(Reservation.created_at.desc())
        .limit(50)
    )
    return list(result.scalars().all())


@router.post("/sweep-expired")
async def sweep_expired(
    _: CurrentUser = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Release every hold past its expiry.

    Exposed as an endpoint so the timeout branch can be demonstrated without
    waiting 20 minutes. In production the background sweeper calls the same
    repository function on a timer.
    """
    expired = await reservation_repo.expire_due_holds(session)
    return {"released": len(expired), "reservation_ids": [str(i) for i in expired]}
