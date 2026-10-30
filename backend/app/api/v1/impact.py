"""Impact and admin endpoints.

`/impact/definitions` exists so a reviewer can check what every number on the
dashboard actually means without reading the source. A metric whose definition
is not published is not a measurement.
"""

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user, require_active_user, require_admin
from app.core.errors import ValidationFailed
from app.models import SafetyCheck, User
from app.schemas.auth import CurrentUser
from app.schemas.impact import MetricDefinition
from app.services import donation as donation_service
from app.services import impact as service

router = APIRouter(tags=["impact"])


@router.get("/impact/report")
async def impact_report(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    donor_org_id: uuid.UUID | None = Query(default=None),
    food_category: str | None = Query(default=None),
    _: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Metrics with their definitions, provenance and limitations.

    Every metric carries its formula and denominator, and the report states
    plainly how much of the underlying data is simulated.
    """
    return await service.build_report(
        session,
        date_from=date_from,
        date_to=date_to,
        donor_org_id=str(donor_org_id) if donor_org_id else None,
        food_category=food_category,
    )


@router.get("/impact/definitions", response_model=list[MetricDefinition])
async def metric_definitions() -> list[MetricDefinition]:
    """What every metric means. Public, deliberately — a number whose
    definition is hidden cannot be checked."""
    return list(service.DEFINITIONS.values())


@router.get("/impact/narrative")
async def impact_narrative(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    _: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """A sentence or two over the computed figures.

    The LLM receives finished numbers and writes prose. It performs no
    arithmetic — `compute_metrics` has already run by the time it is called.
    """
    metrics, _row = await service.compute_metrics(
        session, date_from=date_from, date_to=date_to
    )
    narrative = await service.narrate(session, metrics)
    return {
        "narrative": narrative,
        "generated_from": [m.key for m in metrics],
        "note": (
            "Generated from deterministic figures. The language model wrote "
            "the sentences; it did not compute the numbers."
        ),
    }


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/safety-queue")
async def safety_queue(
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Donations awaiting human safety review.

    Nothing auto-resolves out of this queue. A NEEDS_REVIEW donation waits
    here until a person decides, which is the entire point of the outcome
    existing as a distinct state.
    """
    result = await session.execute(
        text(
            """
            SELECT sc.id AS check_id, sc.donation_id, sc.outcome, sc.unevaluated,
                   sc.created_at, d.title, d.food_category, d.quantity_total,
                   d.unit, d.prepared_at, d.pickup_window_start,
                   o.name AS donor_name, o.is_verified AS donor_verified
              FROM safety_checks sc
              JOIN donations d ON d.id = sc.donation_id
              JOIN organizations o ON o.id = d.donor_org_id
             WHERE sc.outcome = 'NEEDS_REVIEW'
               AND sc.review_outcome IS NULL
             ORDER BY sc.created_at ASC
             LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings()]


@admin_router.post("/safety-queue/{donation_id}/resolve")
async def resolve_safety_review(
    donation_id: uuid.UUID,
    approve: bool = Query(description="true to mark ELIGIBLE, false to REJECT"),
    notes: str = Query(min_length=3, max_length=2000),
    reviewer: User = Depends(require_active_user),
    _: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Resolve a review. Attributed, and never automatic.

    A REJECTED donation cannot be approved here: a hard rule failure is not
    appealable by anyone, which is what "hard" means.
    """
    check = await donation_service.resolve_review(
        session,
        donation_id=donation_id,
        reviewer=reviewer,
        approve=approve,
        notes=notes,
    )
    return {
        "donation_id": str(donation_id),
        "outcome": check.review_outcome.value if check.review_outcome else None,
        "reviewed_by": str(reviewer.id),
        "reviewed_at": check.reviewed_at.isoformat() if check.reviewed_at else None,
    }


@admin_router.get("/audit/donations/{donation_id}")
async def donation_audit(
    donation_id: uuid.UUID,
    _: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Everything recorded about one donation, for an auditor."""
    donation = await donation_service.get_donation(session, donation_id)

    checks = await session.execute(
        select(SafetyCheck)
        .where(SafetyCheck.donation_id == donation_id)
        .order_by(SafetyCheck.created_at)
    )
    reservations = await session.execute(
        text(
            "SELECT id, recipient_org_id, quantity, status, expires_at, "
            "released_at, release_reason, created_at "
            "FROM reservations WHERE donation_id = :id ORDER BY created_at"
        ),
        {"id": donation_id},
    )
    handovers = await session.execute(
        text(
            """
            SELECT h.id, h.kind, h.quantity, h.code_verified, h.occurred_at,
                   h.actor_user_id
              FROM handover_events h
              JOIN pickups p ON p.id = h.pickup_id
              JOIN reservations r ON r.id = p.reservation_id
             WHERE r.donation_id = :id
             ORDER BY h.occurred_at
            """
        ),
        {"id": donation_id},
    )

    return {
        "donation": {
            "id": str(donation.id),
            "title": donation.title,
            "status": donation.status.value,
            "safety_status": donation.safety_status.value,
            "quantity_total": str(donation.quantity_total),
            "quantity_reserved": str(donation.quantity_reserved),
            "quantity_collected": str(donation.quantity_collected),
            "quantity_delivered": str(donation.quantity_delivered),
            "data_source": donation.data_source.value,
        },
        "safety_checks": [
            {
                "id": str(c.id),
                "outcome": c.outcome.value,
                "unevaluated": c.unevaluated,
                "per_rule": c.per_rule,
                "reviewed_by": str(c.reviewed_by_user_id) if c.reviewed_by_user_id else None,
                "created_at": c.created_at.isoformat(),
            }
            for c in checks.scalars().all()
        ],
        "reservations": [dict(r) for r in reservations.mappings()],
        "handover_events": [dict(h) for h in handovers.mappings()],
    }


@admin_router.post("/flush-outbox")
async def flush_outbox(
    _: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Attempt delivery of queued notifications.

    Exposed so the outbox can be demonstrated. The background sweeper calls
    the same function on a timer.
    """
    from app.providers.notify import flush_outbox as flush

    sent, failed = await flush(session)
    return {"sent": sent, "failed": failed}
