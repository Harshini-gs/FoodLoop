"""Recipient matching: a deterministic filter, then a transparent score.

No LLM call appears anywhere in this file, by design. Distances come from
PostGIS, capacity and history from table columns. An LLM asked to estimate
"how far is this NGO" will produce a confident number that is simply wrong,
and nothing downstream would catch it.

Two stages:

  1. **Filter** (SQL) — remove candidates that are ineligible on hard
     criteria. Every exclusion is recorded with a human-readable reason, so a
     donor can see why a nearby NGO was not offered the food. Silence here
     looks like a bug to a user.

  2. **Score** (Python, pure) — rank the survivors on four components, and
     store the breakdown. "0.82" alone is unusable; "2.1 km away, weight 0.30"
     can be shown in a UI and argued with.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import get_settings

log = get_logger(__name__)

# Weights must sum to 1.0 — asserted at import time, so a careless edit fails
# fast rather than silently producing scores above 1.
WEIGHTS: dict[str, float] = {
    "proximity": 0.30,
    "capacity_fit": 0.25,
    "window_slack": 0.20,
    "reliability": 0.25,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "matching weights must sum to 1.0"


@dataclass(frozen=True)
class ScoredCandidate:
    recipient_org_id: uuid.UUID
    recipient_name: str
    score: float
    components: list[dict[str, Any]]
    distance_meters: float
    allocatable_quantity: Decimal

    def to_components_json(self) -> dict[str, Any]:
        return {c["name"]: c for c in self.components}


@dataclass(frozen=True)
class ExcludedCandidate:
    recipient_org_id: uuid.UUID
    recipient_name: str
    reason: str


# ---------------------------------------------------------------------------
# scoring components — pure functions, each testable in isolation
# ---------------------------------------------------------------------------

def proximity_score(distance_m: float, radius_m: float) -> float:
    """1.0 at the donor's door, 0.0 at the edge of the search radius."""
    if radius_m <= 0:
        return 0.0
    return max(0.0, 1.0 - min(distance_m / radius_m, 1.0))


def capacity_fit_score(donation_qty: Decimal, daily_capacity: Decimal | None) -> float:
    """How well the offer fits what this recipient can actually use.

    Rewards a good two-way match. A recipient who can take exactly the offered
    amount scores 1.0. One who could take ten times as much is slightly
    penalised: the food is better placed where it is more fully needed, and
    without this term the largest organization always wins every match.
    """
    if daily_capacity is None or daily_capacity <= 0:
        return 0.5   # unknown capacity: neutral, neither rewarded nor punished

    donation = float(donation_qty)
    capacity = float(daily_capacity)
    if donation <= 0:
        return 0.0

    fits = min(donation, capacity) / donation            # 1.0 if it all fits
    oversize = max(0.0, (capacity - donation) / capacity) * 0.2
    return max(0.0, fits - oversize)


def window_slack_score(
    window_start: datetime, window_end: datetime,
    opens_at: Any | None, closes_at: Any | None,
    tz_name: str,
) -> tuple[float, float]:
    """How many minutes of the pickup window fall inside opening hours.

    Returns (score, overlap_minutes). Capped at one hour: beyond that, more
    slack does not make the match meaningfully better.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    window_minutes = (window_end - window_start).total_seconds() / 60.0

    if opens_at is None or closes_at is None:
        return 0.5, window_minutes   # hours unknown: neutral

    start_local = window_start.astimezone(tz).time()
    end_local = window_end.astimezone(tz).time()

    def minutes(t: Any) -> int:
        return t.hour * 60 + t.minute

    w_start, w_end = minutes(start_local), minutes(end_local)
    o_start, o_end = minutes(opens_at), minutes(closes_at)

    # Normalize both intervals onto a 48-hour line so a midnight wrap becomes
    # a simple overlap calculation instead of a special case.
    if w_end < w_start:
        w_end += 24 * 60
    if o_end < o_start:
        o_end += 24 * 60

    overlap = max(0, min(w_end, o_end) - max(w_start, o_start))
    if overlap == 0:
        # Try the opening hours shifted a day forward, for a window that
        # starts late at night.
        overlap = max(0, min(w_end, o_end + 24 * 60) - max(w_start, o_start + 24 * 60))

    return min(overlap / 60.0, 1.0), float(overlap)


def reliability_score(offers_received: int, offers_accepted: int) -> float:
    """Wilson score lower bound on the acceptance rate.

    A raw average would rank an organization with 1 of 1 accepted above one
    with 47 of 50, which is obviously wrong — the first has told us almost
    nothing. The Wilson lower bound discounts for sample size, so confidence
    has to be earned.

    With no history at all, returns a neutral 0.5 rather than 0. A new
    organization should not be punished for being new; it should be unproven.
    """
    n = offers_received
    if n <= 0:
        return 0.5

    accepted = min(offers_accepted, n)
    p = accepted / n
    z = 1.96                       # 95% confidence

    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


# ---------------------------------------------------------------------------
# stage 1: the deterministic filter
# ---------------------------------------------------------------------------

CANDIDATE_SQL = text(
    """
    SELECT
        o.id,
        o.name,
        o.accepted_categories,
        o.has_refrigeration,
        o.daily_capacity,
        o.opens_at,
        o.closes_at,
        o.offers_received,
        o.offers_accepted,
        o.last_received_at,
        ST_Distance(o.location, d.pickup_location) AS distance_m
      FROM organizations o
      CROSS JOIN (SELECT pickup_location FROM donations WHERE id = :donation_id) d
     WHERE o.kind = 'RECIPIENT'
       AND o.is_active
       AND o.is_verified
       AND ST_DWithin(o.location, d.pickup_location, :radius_m)
     ORDER BY distance_m
     LIMIT 50
    """
)


async def find_candidates(
    session: AsyncSession,
    *,
    donation_id: uuid.UUID,
    food_category: str,
    quantity: Decimal,
    window_start: datetime,
    window_end: datetime,
    radius_m: int | None = None,
    needs_refrigeration: bool = False,
) -> tuple[list[ScoredCandidate], list[ExcludedCandidate], int]:
    """Discover, filter and rank. Returns (kept, excluded, radius_used)."""
    settings = get_settings()
    radius_m = radius_m or settings.search_radius_meters_default

    rows = await session.execute(
        CANDIDATE_SQL, {"donation_id": donation_id, "radius_m": radius_m}
    )

    kept: list[ScoredCandidate] = []
    excluded: list[ExcludedCandidate] = []

    for row in rows.mappings():
        org_id, name = row["id"], row["name"]
        accepted = [str(c) for c in (row["accepted_categories"] or [])]

        # --- hard exclusions, each with a reason the donor can read ---
        if food_category not in accepted:
            excluded.append(ExcludedCandidate(
                org_id, name, f"does not accept {food_category}"
            ))
            continue

        if needs_refrigeration and not row["has_refrigeration"]:
            excluded.append(ExcludedCandidate(
                org_id, name, "no refrigeration, required for this food category"
            ))
            continue

        capacity = row["daily_capacity"]
        if capacity is not None and Decimal(capacity) <= 0:
            excluded.append(ExcludedCandidate(org_id, name, "capacity is zero"))
            continue

        # --- scoring ---
        distance_m = float(row["distance_m"])
        slack_score, overlap_minutes = window_slack_score(
            window_start, window_end, row["opens_at"], row["closes_at"],
            settings.default_timezone,
        )

        if overlap_minutes == 0:
            excluded.append(ExcludedCandidate(
                org_id, name,
                f"closed during the pickup window (open {row['opens_at']}-{row['closes_at']})",
            ))
            continue

        components = [
            {
                "name": "proximity",
                "value": round(proximity_score(distance_m, radius_m), 4),
                "weight": WEIGHTS["proximity"],
                "raw": {"distance_m": round(distance_m, 1), "radius_m": radius_m},
                "explanation": f"{distance_m / 1000:.1f} km from the donor",
            },
            {
                "name": "capacity_fit",
                "value": round(capacity_fit_score(quantity, capacity), 4),
                "weight": WEIGHTS["capacity_fit"],
                "raw": {
                    "donation_qty": str(quantity),
                    "daily_capacity": str(capacity) if capacity is not None else None,
                },
                "explanation": (
                    f"can take {capacity}/day against {quantity} offered"
                    if capacity is not None else "daily capacity not recorded"
                ),
            },
            {
                "name": "window_slack",
                "value": round(slack_score, 4),
                "weight": WEIGHTS["window_slack"],
                "raw": {"overlap_minutes": round(overlap_minutes, 1)},
                "explanation": f"{overlap_minutes:.0f} min of overlap with opening hours",
            },
            {
                "name": "reliability",
                "value": round(
                    reliability_score(row["offers_received"], row["offers_accepted"]), 4
                ),
                "weight": WEIGHTS["reliability"],
                "raw": {
                    "offers_received": row["offers_received"],
                    "offers_accepted": row["offers_accepted"],
                },
                "explanation": (
                    f"accepted {row['offers_accepted']} of {row['offers_received']} past offers"
                    if row["offers_received"] else "no history yet"
                ),
            },
        ]

        score = sum(c["value"] * c["weight"] for c in components)

        allocatable = (
            min(quantity, Decimal(capacity)) if capacity is not None else quantity
        )

        kept.append(ScoredCandidate(
            recipient_org_id=org_id,
            recipient_name=name,
            score=round(score, 4),
            components=components,
            distance_meters=round(distance_m, 1),
            allocatable_quantity=allocatable,
        ))

    kept.sort(key=lambda c: c.score, reverse=True)

    log.info(
        "candidates ranked",
        extra={"context": {
            "donation_id": str(donation_id),
            "kept": len(kept),
            "excluded": len(excluded),
            "radius_m": radius_m,
        }},
    )
    return kept, excluded, radius_m


async def find_candidates_widening(
    session: AsyncSession,
    *,
    donation_id: uuid.UUID,
    food_category: str,
    quantity: Decimal,
    window_start: datetime,
    window_end: datetime,
    needs_refrigeration: bool = False,
    max_widenings: int | None = None,
) -> tuple[list[ScoredCandidate], list[ExcludedCandidate], int, int]:
    """Search, widening the radius if nothing is found.

    Returns (kept, excluded, radius_used, widenings_applied). Bounded by
    MAX_SEARCH_WIDENINGS so this cannot loop forever — the same discipline the
    workflow's cycles use.
    """
    settings = get_settings()
    max_widenings = (
        max_widenings if max_widenings is not None else settings.max_search_widenings
    )
    radius = settings.search_radius_meters_default

    kept: list[ScoredCandidate] = []
    excluded: list[ExcludedCandidate] = []

    for widening in range(max_widenings + 1):
        kept, excluded, radius_used = await find_candidates(
            session,
            donation_id=donation_id,
            food_category=food_category,
            quantity=quantity,
            window_start=window_start,
            window_end=window_end,
            radius_m=radius,
            needs_refrigeration=needs_refrigeration,
        )
        if kept:
            return kept, excluded, radius_used, widening
        radius = int(radius * settings.search_radius_widen_factor)

    return kept, excluded, radius, max_widenings


async def persist_matches(
    session: AsyncSession,
    *,
    donation_id: uuid.UUID,
    candidates: list[ScoredCandidate],
) -> None:
    """Write the ranking, with each score breakdown, to the matches table.

    Upserts on (donation_id, recipient_org_id) so re-ranking after a decline
    updates the existing row rather than violating the unique index.
    """
    now = datetime.now(UTC)
    for rank, candidate in enumerate(candidates, start=1):
        await session.execute(
            text(
                """
                INSERT INTO matches
                    (id, donation_id, recipient_org_id, rank, score,
                     score_components, distance_meters, status, proposed_at)
                VALUES
                    (gen_random_uuid(), :donation_id, :org_id, :rank, :score,
                     CAST(:components AS jsonb), :distance, 'PROPOSED', :now)
                ON CONFLICT (donation_id, recipient_org_id) DO UPDATE
                    SET rank = EXCLUDED.rank,
                        score = EXCLUDED.score,
                        score_components = EXCLUDED.score_components,
                        distance_meters = EXCLUDED.distance_meters
                """
            ),
            {
                "donation_id": donation_id,
                "org_id": candidate.recipient_org_id,
                "rank": rank,
                "score": candidate.score,
                "components": __import__("json").dumps(candidate.to_components_json()),
                "distance": candidate.distance_meters,
                "now": now,
            },
        )
    await session.commit()
