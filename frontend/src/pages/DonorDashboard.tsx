/**
 * Donor dashboard.
 *
 * The free-text box is the point: it is where the one LLM-driven step in the
 * whole application is visible. Extraction returns a DRAFT, and anything it
 * could not find comes back in `missing_fields` with a question. The donor
 * fills the gaps — nothing is invented, which is why a missing preparation
 * time cannot slip past the safety gate.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError, donations, handover, matching, workflow } from "../api/client";
import type { Candidate, Donation, DonationDraft, MatchingResult, SafetyCheck } from "../api/types";
import {
  Card,
  EmptyState,
  ErrorBox,
  Field,
  PolicyNotice,
  ProvenanceTag,
  SafetyBadge,
  Spinner,
  StatusPill,
  formatDateTime,
  formatDistance,
  formatQuantity,
} from "../components/common";

const EXAMPLE_TEXT =
  "We have 50 extra veg meal boxes, pickup between 8 and 9 PM tonight.";

export function DonorDashboard() {
  const [list, setList] = useState<Donation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rawText, setRawText] = useState(EXAMPLE_TEXT);
  const [draft, setDraft] = useState<DonationDraft | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setList(await donations.mine());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "could not load donations");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleExtract() {
    setExtracting(true);
    setDraft(null);
    try {
      setDraft(await donations.extract(rawText));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "extraction failed");
    } finally {
      setExtracting(false);
    }
  }

  return (
    <div className="page">
      <Card
        title="List surplus food"
        subtitle="Type it as you would say it. The system extracts what it can and asks for the rest."
      >
        <Field label="What is available?">
          <textarea
            id="raw-text"
            rows={3}
            value={rawText}
            onChange={(e) => setRawText(e.target.value)}
            placeholder={EXAMPLE_TEXT}
          />
        </Field>

        <div className="row">
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleExtract}
            disabled={extracting || rawText.trim().length < 10}
          >
            {extracting ? "Reading…" : "Extract details"}
          </button>
          <span className="hint">
            This is the only step a language model performs.
          </span>
        </div>

        {draft && <DraftReview draft={draft} />}
      </Card>

      <Card title="Your donations" actions={
        <button type="button" className="btn btn-sm" onClick={() => void refresh()}>
          Refresh
        </button>
      }>
        {error && <ErrorBox message={error} onRetry={() => void refresh()} />}
        {!error && list === null && <Spinner label="Loading your donations" />}
        {!error && list !== null && list.length === 0 && (
          <EmptyState
            title="No donations yet"
            hint="Run the seed command, or list something above."
          />
        )}
        {list !== null && list.length > 0 && (
          <div className="donation-list">
            {list.map((donation) => (
              <DonationRow
                key={donation.id}
                donation={donation}
                expanded={selected === donation.id}
                onToggle={() =>
                  setSelected(selected === donation.id ? null : donation.id)
                }
                onChanged={() => void refresh()}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

/** What extraction found, and — more importantly — what it did not. */
function DraftReview({ draft }: { draft: DonationDraft }) {
  const complete = draft.missing_fields.length === 0;

  return (
    <div className={`draft ${complete ? "draft-ok" : "draft-gaps"}`}>
      <h3>{complete ? "Extracted" : "Needs more information"}</h3>

      <dl className="draft-fields">
        <Row label="Description" value={draft.title} />
        <Row label="Category" value={draft.food_category} />
        <Row
          label="Quantity"
          value={
            draft.quantity_total
              ? formatQuantity(draft.quantity_total, draft.unit ?? undefined)
              : null
          }
        />
        <Row label="Window opens" value={formatDateTime(draft.pickup_window_start)} />
        <Row label="Window closes" value={formatDateTime(draft.pickup_window_end)} />
        <Row label="Prepared at" value={formatDateTime(draft.prepared_at)} />
      </dl>

      {draft.missing_fields.length > 0 && (
        <div className="box box-warn">
          <strong>Missing: {draft.missing_fields.join(", ")}</strong>
          {draft.clarification_question && <p>{draft.clarification_question}</p>}
          <p className="small">
            These are left empty rather than guessed. A fabricated preparation
            time would walk straight through the safety check.
          </p>
        </div>
      )}

      {draft.ambiguities.length > 0 && (
        <div className="box box-info">
          <strong>Assumptions worth checking</strong>
          <ul>
            {draft.ambiguities.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {draft.unit_normalizations.length > 0 && (
        <p className="small">
          Normalized: {draft.unit_normalizations.join(" · ")}
        </p>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string | null }) {
  return (
    <>
      <dt>{label}</dt>
      <dd className={value ? "" : "missing"}>{value ?? "not stated"}</dd>
    </>
  );
}

function DonationRow({
  donation,
  expanded,
  onToggle,
  onChanged,
}: {
  donation: Donation;
  expanded: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const remaining =
    Number(donation.quantity_total) - Number(donation.quantity_reserved);

  return (
    <article className="donation">
      <button type="button" className="donation-head" onClick={onToggle}>
        <div className="donation-title">
          <strong>{donation.title}</strong>
          <span className="donation-meta">
            {formatQuantity(donation.quantity_total, donation.unit)} ·{" "}
            {formatDateTime(donation.pickup_window_start)}
          </span>
        </div>
        <div className="donation-badges">
          <SafetyBadge status={donation.safety_status} />
          <StatusPill status={donation.status} />
          <ProvenanceTag source={donation.data_source} />
        </div>
      </button>

      {expanded && (
        <div className="donation-detail">
          <div className="quantity-chain">
            <QuantityStep label="Offered" value={donation.quantity_total} unit={donation.unit} />
            <QuantityStep label="Reserved" value={donation.quantity_reserved} unit={donation.unit} />
            <QuantityStep label="Collected" value={donation.quantity_collected} unit={donation.unit} />
            <QuantityStep label="Delivered" value={donation.quantity_delivered} unit={donation.unit} emphasis />
          </div>
          <p className="small">
            Each step is capped at the one before it by a database CHECK
            constraint, so delivered can never exceed collected. {remaining} {donation.unit.toLowerCase()} still unreserved.
          </p>

          <DonationActions donation={donation} onChanged={onChanged} />
        </div>
      )}
    </article>
  );
}

function QuantityStep({
  label,
  value,
  unit,
  emphasis,
}: {
  label: string;
  value: string;
  unit: string;
  emphasis?: boolean;
}) {
  return (
    <div className={`qstep ${emphasis ? "qstep-emphasis" : ""}`}>
      <span className="qstep-label">{label}</span>
      <span className="qstep-value">{formatQuantity(value, unit)}</span>
    </div>
  );
}

function DonationActions({
  donation,
  onChanged,
}: {
  donation: Donation;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [safety, setSafety] = useState<SafetyCheck | null>(null);
  const [candidates, setCandidates] = useState<MatchingResult | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label);
    setMessage(null);
    try {
      await fn();
    } catch (err) {
      setMessage(err instanceof ApiRequestError ? err.message : "action failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="actions">
      <div className="row">
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy !== null}
          onClick={() =>
            run("safety", async () => {
              setSafety(await donations.evaluateSafety(donation.id));
              onChanged();
            })
          }
        >
          {busy === "safety" ? "Checking…" : "Run safety check"}
        </button>

        <button
          type="button"
          className="btn btn-sm"
          disabled={busy !== null || donation.safety_status !== "ELIGIBLE"}
          title={
            donation.safety_status !== "ELIGIBLE"
              ? "The donation must clear safety first"
              : undefined
          }
          onClick={() =>
            run("match", async () => {
              setCandidates(await matching.candidates(donation.id, true));
            })
          }
        >
          {busy === "match" ? "Ranking…" : "Find recipients"}
        </button>

        <button
          type="button"
          className="btn btn-sm"
          disabled={busy !== null}
          onClick={() =>
            run("workflow", async () => {
              await workflow.run(donation.id);
              onChanged();
              setMessage("Workflow run complete — see the Workflow tab.");
            })
          }
        >
          {busy === "workflow" ? "Running…" : "Run workflow"}
        </button>
      </div>

      {message && <p className="small">{message}</p>}

      {safety && <SafetyDetail check={safety} />}
      {candidates && <CandidateList result={candidates} />}
      {code && (
        <div className="box box-info">
          <strong>Delivery code: {code}</strong>
          <p className="small">
            Generated when the collection was recorded. The recipient enters
            this to confirm what actually arrived — which is why no automated
            process can close a donation.
          </p>
        </div>
      )}

      <CollectForm donationId={donation.id} onCode={setCode} onChanged={onChanged} />
    </div>
  );
}

function SafetyDetail({ check }: { check: SafetyCheck }) {
  return (
    <div className="safety-detail">
      <h4>
        Safety: <SafetyBadge status={check.outcome} />
      </h4>
      <PolicyNotice policyStatus={check.policy_status} />

      <table className="rules">
        <thead>
          <tr>
            <th>Rule</th>
            <th>Severity</th>
            <th>Result</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {check.per_rule.map((rule) => (
            <tr key={rule.rule_id} className={resultClass(rule.passed)}>
              <td><code>{rule.rule_id}</code></td>
              <td>{rule.severity}</td>
              <td>
                {rule.passed === true && "passed"}
                {rule.passed === false && "failed"}
                {rule.passed === null && "not evaluated"}
              </td>
              <td>
                {rule.reason}
                {rule.limit && <span className="small"> (limit: {rule.limit})</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {check.unevaluated && check.unevaluated.length > 0 && (
        <p className="small">
          <strong>{check.unevaluated.length} rule(s) could not be evaluated.</strong>{" "}
          Missing evidence sends a donation to human review — it never counts as
          a pass.
        </p>
      )}
    </div>
  );
}

function resultClass(passed: boolean | null): string {
  if (passed === true) return "rule-pass";
  if (passed === false) return "rule-fail";
  return "rule-unknown";
}

function CandidateList({ result }: { result: MatchingResult }) {
  return (
    <div className="candidates">
      <h4>
        {result.candidates.length} eligible recipient(s)
        <span className="small">
          {" "}
          within {(result.search_radius_meters / 1000).toFixed(1)} km
          {result.widenings_applied > 0 &&
            ` after ${result.widenings_applied} widening(s)`}
        </span>
      </h4>

      {result.candidates.map((candidate) => (
        <CandidateCard key={candidate.recipient_org_id} candidate={candidate} />
      ))}

      {result.excluded.length > 0 && (
        <details className="excluded">
          <summary>{result.excluded.length} excluded — and why</summary>
          <ul>
            {result.excluded.map((item) => (
              <li key={item.recipient_org_id}>
                <strong>{item.recipient_name}</strong>: {item.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** The score breakdown. "0.82" alone is unusable; the components are not. */
function CandidateCard({ candidate }: { candidate: Candidate }) {
  return (
    <div className="candidate">
      <div className="candidate-head">
        <strong>
          #{candidate.rank} {candidate.recipient_name}
        </strong>
        <span className="candidate-score">{candidate.score.toFixed(3)}</span>
      </div>
      <p className="small">
        {formatDistance(candidate.distance_meters)} away · can take{" "}
        {candidate.allocatable_quantity}
      </p>
      <ul className="components">
        {candidate.components.map((component) => (
          <li key={component.name}>
            <span className="component-name">{component.name}</span>
            <span className="component-bar">
              <span
                className="component-fill"
                style={{ width: `${Math.round(component.value * 100)}%` }}
              />
            </span>
            <span className="component-value">
              {component.value.toFixed(2)} × {component.weight}
            </span>
            <span className="component-why">{component.explanation}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The donor confirms collection, reading the code to the volunteer. */
function CollectForm({
  donationId,
  onCode,
  onChanged,
}: {
  donationId: string;
  onCode: (code: string) => void;
  onChanged: () => void;
}) {
  const [pickupId, setPickupId] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [quantity, setQuantity] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  async function loadPickup() {
    try {
      const pickup = await handover.forDonation(donationId);
      if (pickup) {
        setPickupId(pickup.id);
        setStatus(`Pickup ${pickup.status.toLowerCase()}`);
      } else {
        setStatus("No pickup scheduled yet");
      }
    } catch (err) {
      setStatus(err instanceof ApiRequestError ? err.message : "could not load");
    }
  }

  async function confirmCollection() {
    if (!pickupId) return;
    try {
      const event = await handover.collect(pickupId, code, quantity);
      setStatus(`Collection recorded: ${event.quantity}`);
      // The backend generates the DELIVERY code at collection time and appends
      // it to the event notes. Surface it so the recipient can confirm receipt.
      const match = event.notes?.match(/\[delivery code:\s*([A-Z0-9]+)\]/);
      if (match) onCode(match[1]);
      onChanged();
    } catch (err) {
      setStatus(err instanceof ApiRequestError ? err.message : "failed");
    }
  }

  return (
    <details className="collect">
      <summary>Confirm collection</summary>
      <div className="collect-body">
        <button type="button" className="btn btn-sm" onClick={() => void loadPickup()}>
          Load pickup
        </button>

        {pickupId && (
          <>
            <Field label="Handover code">
              <input
                id={`code-${donationId}`}
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                maxLength={8}
              />
            </Field>
            <Field label="Quantity actually collected">
              <input
                id={`qty-${donationId}`}
                type="number"
                step="0.01"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            </Field>
            <p className="small">
              Record what actually left the kitchen. If three boxes were
              dropped, enter 47 — the impact report will show 47, not the 50
              that were offered.
            </p>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => void confirmCollection()}
              disabled={code.length < 4 || quantity === ""}
            >
              Record collection
            </button>
          </>
        )}

        {status && <p className="small">{status}</p>}
      </div>
    </details>
  );
}
