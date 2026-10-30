"""Donation schemas, including the structured-extraction contract.

`DonationDraft` is the contract the LLM must satisfy. Two things make it safe:

1. Every extractable field is Optional. The model is structurally unable to
   claim it found a value it did not find — a missing field comes back None and
   its name goes in `missing_fields`.

2. It is validated with Pydantic before anything downstream touches it. An LLM
   returning a malformed or hallucinated shape fails validation and routes to
   the clarification branch rather than polluting the database.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    DataSource,
    DonationStatus,
    FoodCategory,
    QuantityUnit,
    SafetyStatus,
)
from app.schemas.common import GeoPoint, ORMModel

# Fields a donation cannot be submitted for safety evaluation without.
MANDATORY_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "food_category",
        "unit",
        "quantity_total",
        "pickup_window_start",
        "pickup_window_end",
        "pickup_address_text",
    }
)


class DonationDraft(BaseModel):
    """The Surplus agent's output. EVERY field is optional by design."""

    title: str | None = None
    food_category: FoodCategory | None = None
    unit: QuantityUnit | None = None
    quantity_total: Decimal | None = Field(default=None, gt=0)

    prepared_at: datetime | None = None
    pickup_window_start: datetime | None = None
    pickup_window_end: datetime | None = None

    pickup_address_text: str | None = None
    storage_notes: str | None = None
    kept_refrigerated: bool | None = None
    allergen_notes: str | None = None

    # --- extraction bookkeeping ---
    missing_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    # ["30 boxes -> 30 BOXES", "1 PM -> 2026-10-22T13:00+05:30"]
    unit_normalizations: list[str] = Field(default_factory=list)
    # A question to put to the donor when something mandatory is absent.
    clarification_question: str | None = None

    @model_validator(mode="after")
    def _recompute_missing(self) -> "DonationDraft":
        """Recompute missing_fields from the data rather than trusting the
        model's own list. An LLM that forgets to report a gap cannot hide it."""
        actually_missing = sorted(
            field for field in MANDATORY_FIELDS if getattr(self, field, None) is None
        )
        self.missing_fields = actually_missing
        return self

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields


class DonationCreate(BaseModel):
    """Structured form submission. Either this or `raw_description` alone."""

    title: str = Field(min_length=3, max_length=200)
    food_category: FoodCategory
    unit: QuantityUnit
    quantity_total: Decimal = Field(gt=0, le=Decimal("99999999.99"))

    prepared_at: datetime | None = None
    pickup_window_start: datetime
    pickup_window_end: datetime

    pickup_address_text: str = Field(min_length=5)
    pickup_location: GeoPoint

    storage_notes: str | None = None
    kept_refrigerated: bool | None = None
    allergen_notes: str | None = None

    @model_validator(mode="after")
    def _check_window(self) -> "DonationCreate":
        if self.pickup_window_end <= self.pickup_window_start:
            raise ValueError("pickup_window_end must be after pickup_window_start")
        if self.prepared_at and self.prepared_at > self.pickup_window_start:
            raise ValueError("prepared_at cannot be after the pickup window opens")
        return self


class DonationFromText(BaseModel):
    """Free-text submission. This is the path that exercises the LLM step."""

    raw_description: str = Field(min_length=10, max_length=4000)
    # The donor's address is not guessed from text — it comes from their org
    # profile unless overridden here.
    pickup_address_text: str | None = None
    pickup_location: GeoPoint | None = None


class ClarificationAnswer(BaseModel):
    """The donor's reply to a clarification question. Re-enters extraction."""

    answer: str = Field(min_length=1, max_length=2000)


class DonationOut(ORMModel):
    id: uuid.UUID
    donor_org_id: uuid.UUID
    title: str
    food_category: FoodCategory
    unit: QuantityUnit

    quantity_total: Decimal
    quantity_reserved: Decimal
    quantity_collected: Decimal
    quantity_delivered: Decimal

    prepared_at: datetime | None
    pickup_window_start: datetime
    pickup_window_end: datetime

    status: DonationStatus
    safety_status: SafetyStatus
    missing_fields: list[str] | None
    data_source: DataSource
    created_at: datetime

    @property
    def quantity_remaining(self) -> Decimal:
        return self.quantity_total - self.quantity_reserved


class DonationDetail(DonationOut):
    """Adds fields only the donor's own organization and admins may see."""

    raw_description: str | None
    pickup_address_text: str
    storage_notes: str | None
    allergen_notes: str | None
    kept_refrigerated: bool | None
    ambiguities: list[str] | None


class SafetyRuleResult(BaseModel):
    rule_id: str
    severity: str
    passed: bool | None  # None = could not be evaluated
    reason: str
    observed: str | None = None
    limit: str | None = None


class SafetyCheckOut(ORMModel):
    id: uuid.UUID
    donation_id: uuid.UUID
    outcome: SafetyStatus
    per_rule: list[SafetyRuleResult]
    unevaluated: list[str] | None
    created_at: datetime

    # So the UI can state which policy produced this decision.
    rule_version: str | None = None
    policy_status: str | None = None
