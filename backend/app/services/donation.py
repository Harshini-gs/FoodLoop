"""Donation lifecycle: creation, extraction, safety evaluation, cancellation.

The safety evaluation path is the one to read. It computes a decision, writes
an append-only `safety_checks` row pinned to a rule version, and then updates
`donations.safety_status`. That status is what the database trigger reads, so
this function is the only legitimate way the gate ever opens.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.core.logging import get_logger
from app.models import Donation, Organization, SafetyCheck, SafetyRuleVersion, User
from app.models.enums import DataSource, DonationStatus, SafetyStatus
from app.providers.llm import get_llm_provider
from app.schemas.donation import DonationCreate, DonationDraft, DonationFromText
from app.services.safety.engine import (
    SafetyDecision,
    evaluate,
    facts_from_donation,
    get_ruleset,
)

log = get_logger(__name__)


def compute_dedupe_hash(
    *, donor_org_id: uuid.UUID, category: str, quantity: Decimal,
    window_start: datetime,
) -> str:
    """Natural key for duplicate detection.

    Rounded to the hour deliberately: a donor who submits the same listing
    twice within an hour almost certainly double-clicked, whereas the same
    quantity offered tomorrow is a genuinely new donation.
    """
    material = (
        f"{donor_org_id}|{category}|{quantity}|"
        f"{window_start.astimezone(UTC).strftime('%Y-%m-%dT%H')}"
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def find_duplicate(
    session: AsyncSession, *, dedupe_hash: str, exclude_id: uuid.UUID | None = None
) -> Donation | None:
    query = select(Donation).where(
        Donation.dedupe_hash == dedupe_hash,
        Donation.status.notin_(
            [DonationStatus.CANCELLED, DonationStatus.EXPIRED, DonationStatus.REJECTED]
        ),
    )
    if exclude_id:
        query = query.where(Donation.id != exclude_id)
    result = await session.execute(query)
    return result.scalars().first()


async def create_donation(
    session: AsyncSession,
    *,
    payload: DonationCreate,
    donor_org_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    raw_description: str | None = None,
) -> Donation:
    """Create a validated listing. Does NOT evaluate safety — that is a
    separate step, so the two can be tested and reasoned about apart."""
    dedupe_hash = compute_dedupe_hash(
        donor_org_id=donor_org_id,
        category=payload.food_category.value,
        quantity=payload.quantity_total,
        window_start=payload.pickup_window_start,
    )

    duplicate = await find_duplicate(session, dedupe_hash=dedupe_hash)
    if duplicate is not None:
        raise ValidationFailed(
            "an identical donation was already listed within the last hour",
            details={"existing_donation_id": str(duplicate.id)},
        )

    donation = Donation(
        donor_org_id=donor_org_id,
        created_by_user_id=created_by_user_id,
        title=payload.title,
        raw_description=raw_description,
        food_category=payload.food_category,
        unit=payload.unit,
        quantity_total=payload.quantity_total,
        quantity_reserved=Decimal("0.00"),
        quantity_collected=Decimal("0.00"),
        quantity_delivered=Decimal("0.00"),
        prepared_at=payload.prepared_at,
        storage_notes=payload.storage_notes,
        kept_refrigerated=payload.kept_refrigerated,
        allergen_notes=payload.allergen_notes,
        pickup_window_start=payload.pickup_window_start,
        pickup_window_end=payload.pickup_window_end,
        pickup_address_text=payload.pickup_address_text,
        pickup_location=payload.pickup_location.to_wkt(),
        status=DonationStatus.SAFETY_REVIEW,
        safety_status=SafetyStatus.PENDING,
        dedupe_hash=dedupe_hash,
        # A real donor entering real food. Contrast with the seed, which tags
        # everything SIMULATED.
        data_source=DataSource.REAL,
    )
    session.add(donation)
    await session.commit()

    log.info("donation created", extra={"context": {"donation_id": str(donation.id)}})
    return donation


async def extract_from_text(
    payload: DonationFromText, *, now: datetime | None = None
) -> DonationDraft:
    """Run the free-text path through the LLM provider.

    The returned draft is Pydantic-validated, and its validator recomputes
    `missing_fields` from the data rather than trusting what the model
    reported — so a model that forgets to flag a gap cannot hide it.
    """
    provider = get_llm_provider()
    draft = await provider.extract_donation(payload.raw_description, now=now)

    if payload.pickup_address_text:
        draft.pickup_address_text = payload.pickup_address_text
        draft = DonationDraft(**draft.model_dump())   # re-run the validator

    return draft


async def get_donation(session: AsyncSession, donation_id: uuid.UUID) -> Donation:
    result = await session.execute(select(Donation).where(Donation.id == donation_id))
    donation = result.scalars().first()
    if donation is None:
        raise NotFound(f"donation {donation_id} does not exist")
    return donation


async def ensure_rule_version_row(
    session: AsyncSession, *, version: str | None = None
) -> SafetyRuleVersion:
    """Make sure the active ruleset exists as a database row.

    Safety checks reference a rule version so a decision made under demo_v1
    stays explainable after demo_v2 ships. Creating it lazily keeps the seed
    simpler without losing the guarantee.
    """
    ruleset = get_ruleset(version)

    result = await session.execute(
        select(SafetyRuleVersion).where(SafetyRuleVersion.version == ruleset.version)
    )
    row = result.scalars().first()
    if row is not None:
        return row

    row = SafetyRuleVersion(
        version=ruleset.version,
        policy_status=ruleset.policy_status,
        source_note=ruleset.source_note,
        rules=ruleset.raw,
        is_active=True,
    )
    session.add(row)
    await session.commit()
    log.info("rule version recorded", extra={"context": {"version": ruleset.version}})
    return row


async def evaluate_safety(
    session: AsyncSession, *, donation_id: uuid.UUID
) -> tuple[SafetyDecision, SafetyCheck]:
    """Evaluate the safety rules and record the outcome.

    This is the ONLY function that may set `donations.safety_status`. The
    database trigger reads that column, so every route to a reservation passes
    through here.
    """
    donation = await get_donation(session, donation_id)

    org_result = await session.execute(
        select(Organization).where(Organization.id == donation.donor_org_id)
    )
    donor_org = org_result.scalars().first()

    rule_version = await ensure_rule_version_row(session)

    facts = facts_from_donation(donation, donor_org)
    decision = evaluate(facts)

    check = SafetyCheck(
        donation_id=donation.id,
        rule_version_id=rule_version.id,
        outcome=decision.outcome,
        per_rule=decision.to_per_rule(),
        unevaluated=list(decision.unevaluated),
    )
    session.add(check)

    donation.safety_status = decision.outcome
    donation.status = {
        SafetyStatus.ELIGIBLE: DonationStatus.ELIGIBLE,
        SafetyStatus.NEEDS_REVIEW: DonationStatus.SAFETY_REVIEW,
        SafetyStatus.REJECTED: DonationStatus.REJECTED,
        SafetyStatus.PENDING: DonationStatus.SAFETY_REVIEW,
    }[decision.outcome]

    if decision.outcome == SafetyStatus.REJECTED:
        donation.closed_at = datetime.now(UTC)

    await session.commit()

    log.info(
        "safety evaluated",
        extra={"context": {
            "donation_id": str(donation_id),
            "outcome": decision.outcome.value,
            "unevaluated": list(decision.unevaluated),
        }},
    )
    return decision, check


async def resolve_review(
    session: AsyncSession,
    *,
    donation_id: uuid.UUID,
    reviewer: User,
    approve: bool,
    notes: str,
) -> SafetyCheck:
    """An administrator resolves a NEEDS_REVIEW case.

    Only a human reaches this. There is no automatic path from NEEDS_REVIEW to
    ELIGIBLE anywhere in the codebase, and the review is attributed — the
    CHECK constraint on safety_checks refuses a verdict with no reviewer.

    A REJECTED donation cannot be approved here either: a hard rule failure is
    not appealable, by a human or anything else.
    """
    donation = await get_donation(session, donation_id)

    if donation.safety_status == SafetyStatus.REJECTED:
        raise ValidationFailed(
            "this donation failed a hard safety rule and cannot be approved by review",
            details={"donation_id": str(donation_id)},
        )

    if donation.safety_status != SafetyStatus.NEEDS_REVIEW:
        raise ValidationFailed(
            f"donation is {donation.safety_status.value}, not awaiting review",
            details={"donation_id": str(donation_id)},
        )

    result = await session.execute(
        select(SafetyCheck)
        .where(SafetyCheck.donation_id == donation_id)
        .order_by(SafetyCheck.created_at.desc())
        .limit(1)
    )
    check = result.scalars().first()
    if check is None:
        raise NotFound(f"no safety check recorded for donation {donation_id}")

    outcome = SafetyStatus.ELIGIBLE if approve else SafetyStatus.REJECTED

    check.reviewed_by_user_id = reviewer.id
    check.reviewed_at = datetime.now(UTC)
    check.review_notes = notes
    check.review_outcome = outcome

    donation.safety_status = outcome
    donation.status = (
        DonationStatus.ELIGIBLE if approve else DonationStatus.REJECTED
    )
    if not approve:
        donation.closed_at = datetime.now(UTC)

    await session.commit()

    log.info(
        "safety review resolved",
        extra={"context": {
            "donation_id": str(donation_id),
            "approved": approve,
            "reviewer": str(reviewer.id),
        }},
    )
    return check


async def cancel_donation(
    session: AsyncSession, *, donation_id: uuid.UUID, reason: str
) -> Donation:
    """Cancel a donation and release every live hold on it."""
    from app.repositories import reservations as reservation_repo

    donation = await get_donation(session, donation_id)

    if donation.status in (DonationStatus.COMPLETED, DonationStatus.CANCELLED):
        return donation

    live = await session.execute(
        text(
            "SELECT id FROM reservations "
            "WHERE donation_id = :id AND status IN ('HELD', 'CONFIRMED')"
        ),
        {"id": donation_id},
    )
    for row in live.all():
        await reservation_repo.release(
            session, reservation_id=row[0], reason="donation cancelled"
        )

    donation.status = DonationStatus.CANCELLED
    donation.cancel_reason = reason
    donation.closed_at = datetime.now(UTC)
    await session.commit()

    log.info("donation cancelled", extra={"context": {"donation_id": str(donation_id)}})
    return donation


async def list_eligible_donations(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Open offers a recipient may claim.

    Returns the donor's AREA rather than the exact address. A recipient
    browsing the board has no business knowing a restaurant's precise location
    before a handover is agreed — the full address is released with the pickup
    assignment.
    """
    result = await session.execute(
        text(
            """
            SELECT d.id, d.title, d.food_category, d.unit,
                   d.quantity_total, d.quantity_reserved,
                   d.pickup_window_start, d.pickup_window_end,
                   d.status, d.safety_status, d.data_source,
                   o.name AS donor_name,
                   split_part(o.address_text, ',', 2) AS donor_area
              FROM donations d
              JOIN organizations o ON o.id = d.donor_org_id
             WHERE d.safety_status = 'ELIGIBLE'
               AND d.status IN ('ELIGIBLE', 'PARTIALLY_RESERVED')
               AND d.quantity_total > d.quantity_reserved
               AND d.pickup_window_end > now()
             ORDER BY d.pickup_window_end ASC
             LIMIT :limit OFFSET :offset
            """
        ),
        {"limit": limit, "offset": offset},
    )
    return [dict(row) for row in result.mappings()]
