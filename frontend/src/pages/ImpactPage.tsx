/**
 * Impact dashboard.
 *
 * Three rules this screen follows, all of which exist because food-waste
 * projects are notorious for flattering numbers:
 *
 * 1. Offered and delivered are shown side by side and never conflated. The
 *    quantity chain makes the drop-off visible rather than hiding it behind a
 *    single headline figure.
 * 2. Every metric shows its provenance tag and its caveat.
 * 3. A null value renders as "not computable", never as zero. A 0% recovery
 *    rate over zero donations is not a measurement.
 */

import { useEffect, useState } from "react";

import { ApiRequestError, impact } from "../api/client";
import type { ImpactReport, Metric } from "../api/types";
import {
  Card,
  EmptyState,
  ErrorBox,
  ProvenanceTag,
  Spinner,
} from "../components/common";

const CHAIN_KEYS = [
  "quantity_offered",
  "quantity_reserved",
  "quantity_collected",
  "quantity_delivered",
];

export function ImpactPage() {
  const [report, setReport] = useState<ImpactReport | null>(null);
  const [narrative, setNarrative] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    impact
      .report()
      .then(setReport)
      .catch((err) =>
        setError(err instanceof ApiRequestError ? err.message : "could not load"),
      );
    impact
      .narrative()
      .then((data) => setNarrative(data.narrative))
      .catch(() => setNarrative(null));
  }, []);

  if (error) return <div className="page"><ErrorBox message={error} /></div>;
  if (report === null) return <div className="page"><Spinner label="Computing metrics" /></div>;

  const byKey = new Map(report.metrics.map((m) => [m.key, m]));
  const chain = CHAIN_KEYS.map((k) => byKey.get(k)).filter(
    (m): m is Metric => m !== undefined,
  );
  const rest = report.metrics.filter((m) => !CHAIN_KEYS.includes(m.key));

  return (
    <div className="page">
      {report.contains_simulated_data && (
        <div className="box box-warn">
          <strong>This report includes simulated data</strong>
          <p>{report.data_coverage}</p>
        </div>
      )}

      <Card
        title="What was offered, and what actually arrived"
        subtitle="Each step is capped at the one before it by a database constraint."
      >
        {chain.length === 0 ? (
          <EmptyState title="No donations in this period" />
        ) : (
          <div className="chain">
            {chain.map((metric, index) => (
              <div key={metric.key} className="chain-step">
                <span className="chain-label">
                  {metric.label.replace("Quantity ", "")}
                </span>
                <span className="chain-value">
                  {metric.value === null ? "—" : Number(metric.value).toLocaleString()}
                </span>
                {index === chain.length - 1 && (
                  <span className="chain-flag">verified handovers only</span>
                )}
              </div>
            ))}
          </div>
        )}

        <p className="small">
          Delivered comes from <code>handover_events</code>, each requiring a
          confirmation code and an authenticated actor. Offered is what donors
          listed. The two are different columns computed from different tables,
          and the gap between them is the honest part of this dashboard.
        </p>
      </Card>

      {narrative && (
        <Card title="Summary">
          <p>{narrative}</p>
          <p className="small">
            Written by the language model from the finished figures above. It
            performed no arithmetic — the numbers were computed in SQL before it
            was called.
          </p>
        </Card>
      )}

      <Card title="All metrics">
        <table className="metrics">
          <thead>
            <tr>
              <th>Metric</th>
              <th className="num">Value</th>
              <th>Source</th>
              <th>Based on</th>
            </tr>
          </thead>
          <tbody>
            {rest.map((metric) => (
              <MetricRow key={metric.key} metric={metric} />
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Limitations">
        <ul className="limitations">
          {report.limitations.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p className="small">
          Published with the numbers rather than omitted. Generated{" "}
          {new Date(report.generated_at).toLocaleString()}.
        </p>
      </Card>
    </div>
  );
}

function MetricRow({ metric }: { metric: Metric }) {
  const [open, setOpen] = useState(false);

  const display =
    metric.value === null
      ? "not computable"
      : metric.unit === "ratio"
        ? `${(Number(metric.value) * 100).toFixed(1)}%`
        : Number(metric.value).toLocaleString();

  return (
    <>
      <tr className={metric.value === null ? "metric-null" : ""}>
        <td>
          <button type="button" className="metric-name" onClick={() => setOpen(!open)}>
            {metric.label}
          </button>
          {metric.caveat && <span className="small">{metric.caveat}</span>}
        </td>
        <td className="num">{display}</td>
        <td><ProvenanceTag source={metric.provenance} /></td>
        <td className="num small">{metric.sample_size}</td>
      </tr>
      {open && (
        <tr className="metric-definition">
          <td colSpan={4}>
            <dl>
              <dt>Formula</dt>
              <dd><code>{metric.definition.formula}</code></dd>
              {metric.definition.denominator && (
                <>
                  <dt>Denominator</dt>
                  <dd>{metric.definition.denominator}</dd>
                </>
              )}
              <dt>Included</dt>
              <dd>{metric.definition.inclusion_criteria}</dd>
              <dt>From</dt>
              <dd><code>{metric.definition.source_tables.join(", ")}</code></dd>
              {metric.definition.notes && (
                <>
                  <dt>Notes</dt>
                  <dd>{metric.definition.notes}</dd>
                </>
              )}
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}
