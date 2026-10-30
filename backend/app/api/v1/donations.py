"""Donation endpoints: creation (both paths), safety results, cancellation.

Two creation routes exist on purpose:

  POST /donations          structured form -> validated listing
  POST /donations/extract  free text -> a DRAFT the donor confirms

The free-text route deliberately does NOT create a donation. It returns a
draft with `missing_fields` populated, and the donor confirms or completes it.
An LLM extraction that wrote straight to the database would mean a
hallucinated quantity becoming a real listing with nobody in the loop.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user, require_active_user, require_org_membership, require_roles
from app.core.errors import ValidationFailed
from app.models import Donation, SafetyCheck, SafetyRuleVersion, User
from app.models.enums import UserRole
from app.schemas.auth import CurrentUser
from app.schemas.donation import (
    DonationCreate,
    DonationDetail,
    DonationDraft,
    DonationFromText,
    DonationOut,
    SafetyCheckOut,
)
from app.services import donation as service

router = APIRouter(prefix="/donations", tags=["donations"])


@router.post(
    "/extract",
    response_model=DonationDraft,
    dependencies=[Depends(require_roles(UserRole.DONOR, UserRole.ADMIN))],
)
async def extract(payload: DonationFromText) -> DonationDraft:
    """Free text -> a structured draft. Creates nothing.

    This is the one LLM-driven step in the whole application. The result is
    Pydantic-validated and its `missing_fields` are recomputed from the data,
    so a model that silently omits a gap cannot hide it.
    """
    return await service.extract_from_text(payload)


@router.post(
    "",
    response_model=DonationOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.DONOR, UserRole.ADMIN))],
)
async def create(
    payload: DonationCreate,
    raw_description: str | None = Query(
        default=None, description="The donor's original text, kept for audit"
    ),
    user: User = Depends(require_active_user),
    session: AsyncSession = Depends(get_session),
) -> Donation:
    if user.org_id is None:
        raise ValidationFailed("your account is not linked to a donor organization")

    return await service.create_donation(
        session,
        payload=payload,
        donor_org_id=user.org_id,
        created_by_user_id=user.id,
        raw_description=raw_description,
    )


@router.post("/{donation_id}/evaluate-safety", response_model=SafetyCheckOut)
async def evaluate_safety(
    donation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Run the deterministic rules engine and record the outcome.

    The only function in the system that may set `safety_status`, which is
    what the database trigger reads.
    """
    donation = await service.get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)

    decision, check = await service.evaluate_safety(session, donation_id=donation_id)

    return {
        "id": check.id,
        "donation_id": check.donation_id,
        "outcome": check.outcome,
        "per_rule": check.per_rule,
        "unevaluated": check.unevaluated,
        "created_at": check.created_at,
        "rule_version": decision.rule_version,
        # Surfaced so a UI cannot display a verdict without also being able to
        # show that the policy behind it is an illustrative demo.
        "policy_status": decision.policy_status,
    }


@router.get("/{donation_id}/safety", response_model=SafetyCheckOut)
async def latest_safety_check(
    donation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    donation = await service.get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)

    result = await session.execute(
        select(SafetyCheck, SafetyRuleVersion)
        .join(SafetyRuleVersion, SafetyCheck.rule_version_id == SafetyRuleVersion.id)
        .where(SafetyCheck.donation_id == donation_id)
        .order_by(SafetyCheck.created_at.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        raise ValidationFailed("this donation has not been evaluated yet")

    check, version = row
    return {
        "id": check.id,
        "donation_id": check.donation_id,
        "outcome": check.outcome,
        "per_rule": check.per_rule,
        "unevaluated": check.unevaluated,
        "created_at": check.created_at,
        "rule_version": version.version,
        "policy_status": version.policy_status,
    }


@router.get("/mine", response_model=list[DonationOut])
async def my_donations(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    session: AsyncSession = Depends(get_session),
) -> list[Donation]:
    """Donations belonging to the caller's organization. Scoped on the server."""
    if user.org_id is None:
        return []
    result = await session.execute(
        select(Donation)
        .where(Donation.donor_org_id == user.org_id)
        .order_by(Donation.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.get("/available")
async def available_donations(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(require_roles(UserRole.RECIPIENT, UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Open offers a recipient may claim.

    Returns the donor's AREA, not the exact address. The full address is
    released with the pickup assignment, once a handover is actually agreed.
    """
    return await service.list_eligible_donations(session, limit=limit, offset=offset)


@router.get("/{donation_id}", response_model=DonationDetail)
async def get_one(
    donation_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Donation:
    donation = await service.get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)
    return donation


@router.post("/{donation_id}/cancel", response_model=DonationOut)
async def cancel(
    donation_id: uuid.UUID,
    reason: str = Query(min_length=3, max_length=500),
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Donation:
    """Cancel a donation and release every live hold on it."""
    donation = await service.get_donation(session, donation_id)
    require_org_membership(donation.donor_org_id, current)
    return await service.cancel_donation(session, donation_id=donation_id, reason=reason)
