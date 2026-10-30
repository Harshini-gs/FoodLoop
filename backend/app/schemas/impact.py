"""Impact schemas.

The design rule: a metric cannot be returned without its definition. `Metric`
carries the formula, the denominator and a provenance tag, so there is no way
to render a number on a dashboard without also being able to say where it came
from and whether it is measured or estimated.

`quantity_offered` is reported separately from `quantity_delivered` and the two
are never conflated. Counting offered food as recovered food is the specific
dishonesty this project is built to avoid.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import DataSource


class MetricDefinition(BaseModel):
    key: str
    label: str
    # "sum(handover_events.quantity WHERE kind='DELIVER')"
    formula: str
    # What the ratio is over. Stated explicitly because a rate without a
    # denominator is not a measurement.
    denominator: str | None = None
    unit: str
    inclusion_criteria: str
    source_tables: list[str]
    notes: str | None = None


class Metric(BaseModel):
    key: str
    label: str
    value: Decimal | None  # None = not computable from available data
    unit: str
    provenance: DataSource
    definition: MetricDefinition
    # Number of rows the value was computed from. A rate over 3 donations is
    # not the same claim as one over 3000, and the UI should say so.
    sample_size: int
    caveat: str | None = None


class ImpactQuery(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    donor_org_id: str | None = None
    recipient_org_id: str | None = None
    food_category: str | None = None


class ImpactReport(BaseModel):
    generated_at: datetime
    query: ImpactQuery
    metrics: list[Metric]

    # Stated plainly rather than buried: which parts of this report rest on
    # simulated data, and what the report does not support concluding.
    data_coverage: str
    limitations: list[str] = Field(default_factory=list)
    contains_simulated_data: bool


class QuantityBreakdown(BaseModel):
    """The honest chain. Each step is strictly less than or equal to the one
    above it, and that is enforced by a CHECK constraint, not by convention."""

    offered: Decimal
    reserved: Decimal
    collected: Decimal
    delivered: Decimal
    unit: str

    @property
    def recovery_rate(self) -> float | None:
        """delivered / offered. None when nothing was offered — a zero
        denominator is not a 0% recovery rate, it is no measurement."""
        if self.offered == 0:
            return None
        return float(self.delivered / self.offered)
