"""Impact metrics, computed from verified records only.

Two rules govern this module:

1. **A metric cannot be returned without its definition.** Every `Metric`
   carries its formula, denominator, inclusion criteria and source tables. A
   number on a dashboard that nobody can trace is a decoration, not a
   measurement.

2. **Offered is never reported as recovered.** `quantity_offered` comes from
   what donors listed; `quantity_delivered` comes from `handover_events`,
   which require a verified code. They are different columns, computed from
   different tables, and the report shows both.

Rates return None when their denominator is zero. A 0% recovery rate over zero
donations is not a measurement — it is the absence of one, and saying "0%"
would be a lie of arithmetic.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import DataSource
from app.schemas.impact import Metric, MetricDefinition

log = get_logger(__name__)


DEFINITIONS: dict[str, MetricDefinition] = {
    "quantity_offered": MetricDefinition(
        key="quantity_offered",
        label="Quantity offered",
        formula="SUM(donations.quantity_total)",
        denominator=None,
        unit="units (mixed)",
        inclusion_criteria=(
            "Every donation created in the period, whatever its outcome. "
            "This is what donors listed, NOT what was recovered."
        ),
        source_tables=["donations"],
        notes=(
            "Units are mixed (meals, boxes, kg, litres) and are deliberately "
            "not converted — there is no honest conversion between a meal and "
            "a kilogram. Break down by unit before comparing."
        ),
    ),
    "quantity_reserved": MetricDefinition(
        key="quantity_reserved",
        label="Quantity reserved",
        formula="SUM(reservations.quantity) WHERE status IN ('CONFIRMED','CONSUMED')",
        denominator=None,
        unit="units (mixed)",
        inclusion_criteria="Reservations that were confirmed or consumed. Released holds are excluded.",
        source_tables=["reservations"],
    ),
    "quantity_collected": MetricDefinition(
        key="quantity_collected",
        label="Quantity collected",
        formula="SUM(handover_events.quantity) WHERE kind = 'COLLECT'",
        denominator=None,
        unit="units (mixed)",
        inclusion_criteria=(
            "Verified collection events only. Each requires a confirmation "
            "code and an authenticated actor."
        ),
        source_tables=["handover_events"],
    ),
    "quantity_delivered": MetricDefinition(
        key="quantity_delivered",
        label="Quantity delivered",
        formula="SUM(handover_events.quantity) WHERE kind = 'DELIVER'",
        denominator=None,
        unit="units (mixed)",
        inclusion_criteria=(
            "Verified delivery events only. This is the ONLY figure that "
            "represents food actually reaching a recipient."
        ),
        source_tables=["handover_events"],
    ),
    "recovery_rate": MetricDefinition(
        key="recovery_rate",
        label="Recovery rate",
        formula="quantity_delivered / quantity_offered",
        denominator="quantity_offered (sum of donations.quantity_total in the period)",
        unit="ratio",
        inclusion_criteria=(
            "Returns null when nothing was offered. A zero denominator is not "
            "a 0% rate, it is no measurement."
        ),
        source_tables=["donations", "handover_events"],
        notes="Mixed units make this approximate. Treat it as directional.",
    ),
    "donations_completed": MetricDefinition(
        key="donations_completed",
        label="Donations completed",
        formula="COUNT(donations) WHERE status = 'COMPLETED'",
        denominator=None,
        unit="count",
        inclusion_criteria="Donations whose delivery was verified and which are closed.",
        source_tables=["donations"],
    ),
    "donations_created": MetricDefinition(
        key="donations_created",
        label="Donations created",
        formula="COUNT(donations)",
        denominator=None,
        unit="count",
        inclusion_criteria="Every donation created in the period, any outcome.",
        source_tables=["donations"],
    ),
    "pickup_completion_rate": MetricDefinition(
        key="pickup_completion_rate",
        label="Pickup completion rate",
        formula="COUNT(pickups WHERE status='DELIVERED') / COUNT(pickups scheduled)",
        denominator="pickups scheduled in the period",
        unit="ratio",
        inclusion_criteria="Null when no pickups were scheduled.",
        source_tables=["pickups"],
    ),
    "median_time_to_match_minutes": MetricDefinition(
        key="median_time_to_match_minutes",
        label="Median time to match",
        formula="median(first reservation.created_at - donation.created_at)",
        denominator="donations that received at least one reservation",
        unit="minutes",
        inclusion_criteria=(
            "Only donations that were matched. Unmatched donations are "
            "excluded rather than counted as infinite, and their count is "
            "reported separately so the exclusion is visible."
        ),
        source_tables=["donations", "reservations"],
    ),
    "donations_unmatched": MetricDefinition(
        key="donations_unmatched",
        label="Donations never matched",
        formula="COUNT(donations with no reservation row)",
        denominator=None,
        unit="count",
        inclusion_criteria=(
            "Reported alongside time-to-match so the denominator of that "
            "metric is never silently flattering."
        ),
        source_tables=["donations", "reservations"],
    ),
    "total_travel_km": MetricDefinition(
        key="total_travel_km",
        label="Estimated travel distance",
        formula="SUM(pickups.estimated_distance_km)",
        denominator=None,
        unit="km",
        inclusion_criteria="Pickups with a recorded estimate.",
        source_tables=["pickups"],
        notes=(
            "ESTIMATED, not measured. In the default configuration these come "
            "from the simulated routing provider: great-circle distance times "
            "a road factor, which is wrong across a river or a rail line."
        ),
    ),
}


# Always CAST(x AS type) in these statements, never the Postgres `::` operator
# on a bind parameter. SQLAlchemy's text() scans the string for ":name" and
# deliberately skips any match followed by another colon -- that is how it
# avoids mangling "::". So "date_from::timestamptz", written with a leading
# colon, is not recognised as a parameter at all: the statement reaches Postgres
# with the parameter silently missing and fails there, not here. Note also that
# text() does not strip SQL comments, so a colon-prefixed word inside a comment
# becomes a required parameter. Keep notes like this one in Python, not in SQL.
METRICS_SQL = text(
    """
    WITH period AS (
        SELECT CAST(:date_from AS timestamptz) AS start_at,
               CAST(:date_to   AS timestamptz) AS end_at
    ),
    scoped AS (
        SELECT d.* FROM donations d, period p
         WHERE (p.start_at IS NULL OR d.created_at >= p.start_at)
           AND (p.end_at   IS NULL OR d.created_at <  p.end_at)
           AND (CAST(:donor_org_id AS uuid) IS NULL
                OR d.donor_org_id = CAST(:donor_org_id AS uuid))
           AND (CAST(:food_category AS text) IS NULL
                OR CAST(d.food_category AS text) = CAST(:food_category AS text))
    )
    SELECT
        (SELECT COUNT(*) FROM scoped)                                   AS donations_created,
        (SELECT COUNT(*) FROM scoped WHERE status = 'COMPLETED')        AS donations_completed,
        (SELECT COALESCE(SUM(quantity_total), 0) FROM scoped)           AS quantity_offered,
        (SELECT COALESCE(SUM(r.quantity), 0)
           FROM reservations r JOIN scoped s ON s.id = r.donation_id
          WHERE r.status IN ('CONFIRMED', 'CONSUMED'))                  AS quantity_reserved,
        (SELECT COALESCE(SUM(h.quantity), 0)
           FROM handover_events h
           JOIN pickups pk ON pk.id = h.pickup_id
           JOIN reservations r ON r.id = pk.reservation_id
           JOIN scoped s ON s.id = r.donation_id
          WHERE h.kind = 'COLLECT')                                     AS quantity_collected,
        (SELECT COALESCE(SUM(h.quantity), 0)
           FROM handover_events h
           JOIN pickups pk ON pk.id = h.pickup_id
           JOIN reservations r ON r.id = pk.reservation_id
           JOIN scoped s ON s.id = r.donation_id
          WHERE h.kind = 'DELIVER')                                     AS quantity_delivered,
        (SELECT COUNT(*) FROM pickups pk
           JOIN reservations r ON r.id = pk.reservation_id
           JOIN scoped s ON s.id = r.donation_id)                       AS pickups_scheduled,
        (SELECT COUNT(*) FROM pickups pk
           JOIN reservations r ON r.id = pk.reservation_id
           JOIN scoped s ON s.id = r.donation_id
          WHERE pk.status = 'DELIVERED')                                AS pickups_delivered,
        (SELECT COALESCE(SUM(pk.estimated_distance_km), 0) FROM pickups pk
           JOIN reservations r ON r.id = pk.reservation_id
           JOIN scoped s ON s.id = r.donation_id)                       AS total_travel_km,
        (SELECT COUNT(*) FROM scoped s
          WHERE NOT EXISTS (SELECT 1 FROM reservations r WHERE r.donation_id = s.id))
                                                                        AS donations_unmatched,
        (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (first_res.created_at - s.created_at)) / 60.0)
           FROM scoped s
           JOIN LATERAL (
                SELECT r.created_at FROM reservations r
                 WHERE r.donation_id = s.id
                 ORDER BY r.created_at ASC LIMIT 1
           ) first_res ON true)                                         AS median_match_minutes,
        (SELECT COUNT(*) FROM scoped WHERE data_source <> 'REAL')       AS simulated_donations
    """
)


def _metric(
    key: str, value: Decimal | float | int | None, sample_size: int,
    provenance: DataSource = DataSource.REAL, caveat: str | None = None,
) -> Metric:
    definition = DEFINITIONS[key]
    return Metric(
        key=key,
        label=definition.label,
        value=Decimal(str(value)) if value is not None else None,
        unit=definition.unit,
        provenance=provenance,
        definition=definition,
        sample_size=sample_size,
        caveat=caveat,
    )


async def compute_metrics(
    session: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    donor_org_id: str | None = None,
    food_category: str | None = None,
) -> tuple[list[Metric], dict[str, Any]]:
    """Compute the metric set. Returns (metrics, raw_row)."""
    result = await session.execute(
        METRICS_SQL,
        {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "donor_org_id": donor_org_id,
            "food_category": food_category,
        },
    )
    row = dict(result.mappings().one())

    created = int(row["donations_created"])
    offered = Decimal(row["quantity_offered"])
    delivered = Decimal(row["quantity_delivered"])
    pickups_scheduled = int(row["pickups_scheduled"])
    simulated = int(row["simulated_donations"])

    # A zero denominator is not a zero rate.
    recovery_rate = (delivered / offered) if offered > 0 else None
    pickup_rate = (
        Decimal(row["pickups_delivered"]) / Decimal(pickups_scheduled)
        if pickups_scheduled > 0 else None
    )

    simulated_caveat = (
        f"{simulated} of {created} donations in this period are seeded demo data"
        if simulated else None
    )

    metrics = [
        _metric("donations_created", created, created),
        _metric("donations_completed", int(row["donations_completed"]), created),
        _metric("quantity_offered", offered, created, caveat=simulated_caveat),
        _metric("quantity_reserved", Decimal(row["quantity_reserved"]), created),
        _metric("quantity_collected", Decimal(row["quantity_collected"]), created),
        _metric("quantity_delivered", delivered, created, caveat=simulated_caveat),
        _metric(
            "recovery_rate", recovery_rate, created,
            provenance=DataSource.ESTIMATED,
            caveat=(
                "null because nothing was offered in this period"
                if recovery_rate is None
                else "mixed units; directional only"
            ),
        ),
        _metric(
            "pickup_completion_rate", pickup_rate, pickups_scheduled,
            caveat="null because no pickups were scheduled" if pickup_rate is None else None,
        ),
        _metric(
            "median_time_to_match_minutes",
            round(float(row["median_match_minutes"]), 1)
            if row["median_match_minutes"] is not None else None,
            created - int(row["donations_unmatched"]),
            caveat=(
                f"excludes {row['donations_unmatched']} donation(s) that were "
                f"never matched"
            ),
        ),
        _metric("donations_unmatched", int(row["donations_unmatched"]), created),
        _metric(
            "total_travel_km", Decimal(row["total_travel_km"]), pickups_scheduled,
            provenance=DataSource.SIMULATED,
            caveat="simulated routing provider; not measured travel",
        ),
    ]

    return metrics, row


async def build_report(
    session: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    donor_org_id: str | None = None,
    food_category: str | None = None,
) -> dict[str, Any]:
    """The full report, including its own limitations."""
    from app.schemas.impact import ImpactQuery, ImpactReport

    metrics, row = await compute_metrics(
        session, date_from=date_from, date_to=date_to,
        donor_org_id=donor_org_id, food_category=food_category,
    )

    simulated = int(row["simulated_donations"])
    created = int(row["donations_created"])

    limitations = [
        "Quantities mix units (meals, boxes, kg, litres) and are not converted. "
        "There is no honest conversion between a meal and a kilogram.",
        "Travel distances come from the simulated routing provider in the "
        "default configuration and are estimates, not measured journeys.",
        "No environmental impact coefficient is applied. Converting food "
        "weight into CO2e requires a methodology this project does not have, "
        "and inventing one would make the number worse than useless.",
        "Delivered quantities are self-reported by the parties at handover, "
        "verified by a confirmation code rather than independently weighed.",
    ]
    if simulated:
        limitations.insert(
            0,
            f"{simulated} of {created} donations in this period are seeded demo "
            f"data tagged SIMULATED, not real donations.",
        )
    if created == 0:
        limitations.append("No donations in this period; every rate is null.")

    report = ImpactReport(
        generated_at=datetime.now(tz=None).astimezone(),
        query=ImpactQuery(
            date_from=date_from, date_to=date_to,
            donor_org_id=donor_org_id, food_category=food_category,
        ),
        metrics=metrics,
        data_coverage=(
            f"{created} donation(s) in scope, of which {simulated} are "
            f"simulated demo records."
        ),
        limitations=limitations,
        contains_simulated_data=simulated > 0,
    )
    return report.model_dump(mode="json")


async def narrate(session: AsyncSession, metrics: list[Metric]) -> str:
    """Natural-language summary over FINISHED numbers.

    The LLM receives computed values and writes sentences. It does no
    arithmetic, and it cannot: `compute_metrics` has already run and this
    function passes it the results.
    """
    from app.providers.llm import get_llm_provider

    by_key = {m.key: (float(m.value) if m.value is not None else None) for m in metrics}
    return await get_llm_provider().summarize_impact(by_key)
