/**
 * Admin: the safety review queue.
 *
 * Nothing leaves this queue automatically. A NEEDS_REVIEW donation waits here
 * until a person decides, and the decision is attributed in the database — a
 * CHECK constraint on `safety_checks` refuses a verdict with no reviewer.
 *
 * A REJECTED donation does not appear here at all: a hard rule failure is not
 * appealable, which is what "hard" means.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError, admin } from "../api/client";
import {
  Card,
  EmptyState,
  ErrorBox,
  Field,
  Spinner,
  formatDateTime,
  formatQuantity,
} from "../components/common";

interface QueueItem {
  check_id: string;
  donation_id: string;
  outcome: string;
  unevaluated: string[] | null;
  created_at: string;
  title: string;
  food_category: string;
  quantity_total: string;
  unit: string;
  prepared_at: string | null;
  pickup_window_start: string;
  donor_name: string;
  donor_verified: boolean;
}

export function AdminPage() {
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = (await admin.safetyQueue()) as unknown as QueueItem[];
      setQueue(rows);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "could not load the queue");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="page">
      <Card
        title="Safety review queue"
        subtitle="Donations the rules engine could not clear on its own."
        actions={
          <div className="row">
            <button type="button" className="btn btn-sm" onClick={() => void refresh()}>
              Refresh
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() =>
                admin
                  .sweepExpired()
                  .then((r) => setNotice(`${r.released} expired hold(s) released.`))
                  .catch(() => setNotice("sweep failed"))
              }
            >
              Sweep expired holds
            </button>
          </div>
        }
      >
        {notice && <div className="box box-info"><p>{notice}</p></div>}
        {error && <ErrorBox message={error} onRetry={() => void refresh()} />}
        {!error && queue === null && <Spinner label="Loading the queue" />}
        {!error && queue !== null && queue.length === 0 && (
          <EmptyState
            title="Nothing awaiting review"
            hint="Donations appear here when evidence is missing or a soft rule fails."
          />
        )}

        {queue !== null && queue.length > 0 && (
          <div className="queue">
            {queue.map((item) => (
              <QueueRow
                key={item.check_id}
                item={item}
                onResolved={(message) => {
                  setNotice(message);
                  void refresh();
                }}
              />
            ))}
          </div>
        )}

        <p className="small">
          Nothing leaves this queue automatically. There is no timeout and no
          fallback that eventually proceeds — a review that resolved itself
          would make the review decorative.
        </p>
      </Card>
    </div>
  );
}

function QueueRow({
  item,
  onResolved,
}: {
  item: QueueItem;
  onResolved: (message: string) => void;
}) {
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function resolve(approve: boolean) {
    setBusy(true);
    setProblem(null);
    try {
      await admin.resolve(item.donation_id, approve, notes);
      onResolved(
        `${item.title}: ${approve ? "approved" : "rejected"} by review.`,
      );
    } catch (err) {
      setProblem(err instanceof ApiRequestError ? err.message : "failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="queue-item">
      <header>
        <div>
          <strong>{item.title}</strong>
          <span className="small">
            {item.donor_name}
            {!item.donor_verified && " · donor not verified"}
          </span>
        </div>
        <span className="small">{formatDateTime(item.created_at)}</span>
      </header>

      <dl className="queue-facts">
        <dt>Quantity</dt>
        <dd>{formatQuantity(item.quantity_total, item.unit)}</dd>
        <dt>Category</dt>
        <dd>{item.food_category.replace(/_/g, " ").toLowerCase()}</dd>
        <dt>Prepared</dt>
        <dd className={item.prepared_at ? "" : "missing"}>
          {item.prepared_at ? formatDateTime(item.prepared_at) : "not stated"}
        </dd>
        <dt>Window opens</dt>
        <dd>{formatDateTime(item.pickup_window_start)}</dd>
      </dl>

      {item.unevaluated && item.unevaluated.length > 0 && (
        <div className="box box-warn">
          <strong>Could not be evaluated</strong>
          <ul>
            {item.unevaluated.map((rule) => (
              <li key={rule}><code>{rule}</code></li>
            ))}
          </ul>
          <p className="small">
            These rules had no evidence to work with. That is why this is a
            review rather than a pass.
          </p>
        </div>
      )}

      <Field label="Review notes (required, and recorded against your account)">
        <textarea
          id={`notes-${item.check_id}`}
          rows={2}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Called the restaurant — cooked at 18:30, confirmed."
        />
      </Field>

      <div className="row">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={busy || notes.trim().length < 3}
          onClick={() => void resolve(true)}
        >
          Approve
        </button>
        <button
          type="button"
          className="btn btn-sm btn-danger"
          disabled={busy || notes.trim().length < 3}
          onClick={() => void resolve(false)}
        >
          Reject
        </button>
      </div>

      {problem && <p className="form-error">{problem}</p>}
    </article>
  );
}
