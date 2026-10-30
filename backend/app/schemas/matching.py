"""Matching, reservation and handover schemas.

`ScoreComponent` is why the matching agent is defensible. Every component
carries the raw database value it came from, so the UI can show "2.1 km away"
rather than an opaque 0.82, and nobody has to take the ranking on trust.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import (
    DataSource,
    HandoverKind,
    MatchStatus,
    PickupStatus,
    ReservationStatus,
)
from app.schemas.common import ORMModel


class ScoreComponent(BaseModel):
    """One term of the match score, with its provenance."""

    name: str
    value: float = Field(ge=0, le=1, description="Normalized component score")
    weight: float = Field(ge=0, le=1)
    # The database value this was computed from, e.g. {"distance_m": 2140}.
    raw: dict[str, object] = Field(default_factory=dict)
    explanation: str


class CandidateOut(BaseModel):
    recipient_org_id: uuid.UUID
    recipient_name: str
    rank: int
    score: float
    components: list[ScoreComponent]
    distance_meters: float | None = None
    # Quantity this recipient could take, after their capacity is applied.
    allocatable_quantity: Decimal
    # Reasons this candidate was kept. Exclusions are reported separately so a
    # donor can see why a nearby NGO was not offered the food.
    eligibility_notes: list[str] = Field(default_factory=list)


class ExcludedCandidate(BaseModel):
    recipient_org_id: uuid.UUID
    recipient_name: str
    # "does not accept DAIRY", "closed during the pickup window"
    reason: str


class MatchingResult(BaseModel):
    donation_id: uuid.UUID
    search_radius_meters: int
    widenings_applied: int
    candidates: list[CandidateOut]
    excluded: list[ExcludedCandidate]
    evaluated_at: datetime


class MatchOut(ORMModel):
    id: uuid.UUID
    donation_id: uuid.UUID
    recipient_org_id: uuid.UUID
    rank: int
    score: Decimal
    score_components: dict[str, object]
    distance_meters: Decimal | None
    status: MatchStatus
    confirmation_deadline: datetime | None
    proposed_at: datetime


class ReserveRequest(BaseModel):
    donation_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    # Supplied by the caller. A retry with the same key returns the original
    # reservation instead of creating a second one.
    idempotency_key: str | None = Field(default=None, max_length=128)


class ReservationOut(ORMModel):
    id: uuid.UUID
    donation_id: uuid.UUID
    recipient_org_id: uuid.UUID
    quantity: Decimal
    status: ReservationStatus
    expires_at: datetime
    created_at: datetime


class MatchResponse(BaseModel):
    """A recipient accepting or declining an offer."""

    accept: bool
    quantity: Decimal | None = Field(default=None, gt=0)
    decline_reason: str | None = Field(default=None, max_length=500)


class PickupOut(ORMModel):
    id: uuid.UUID
    reservation_id: uuid.UUID
    volunteer_user_id: uuid.UUID | None
    status: PickupStatus
    scheduled_for: datetime | None
    estimated_distance_km: Decimal | None
    estimated_duration_min: int | None
    # So the UI can say "estimated, not live navigation".
    data_source: DataSource
    reassignment_count: int


class PickupScheduled(PickupOut):
    """Returned exactly once, at scheduling time. The only moment the
    plaintext handover code exists outside the donor's screen."""

    handover_code: str = Field(description="Shown once. Only a hash is stored.")


class HandoverRequest(BaseModel):
    """Recording a real-world handover.

    `quantity` is what ACTUALLY moved, which may be less than was reserved.
    A partial handover is a first-class outcome, not an error.
    """

    kind: HandoverKind
    code: str = Field(min_length=4, max_length=16)
    quantity: Decimal = Field(ge=0)
    notes: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=128)


class HandoverOut(ORMModel):
    id: uuid.UUID
    pickup_id: uuid.UUID
    kind: HandoverKind
    quantity: Decimal
    code_verified: bool
    occurred_at: datetime
    notes: str | None
