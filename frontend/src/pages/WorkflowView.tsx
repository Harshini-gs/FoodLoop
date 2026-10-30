/**
 * Workflow view.
 *
 * Every row rendered here is a `workflow_events` row written by a node on
 * entry or exit. Nothing is synthesised, interpolated or animated to make the
 * application look busier than it is — if a step appears, it ran.
 *
 * That constraint is the difference between a workflow visualization and a
 * decoration, and it is the reason this screen is worth showing to a reviewer.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError, donations, workflow } from "../api/client";
import type { Donation, WorkflowEvent, WorkflowEventsResponse } from "../api/types";
import {
  Card,
  EmptyState,
  ErrorBox,
  Spinner,
  StatusPill,
  formatDateTime,
  humanize,
} from "../components/common";

export function WorkflowView() {
  const [list, setList] = useState<Donation[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [data, setData] = useState<WorkflowEventsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [engine, setEngine] = useState<"sequential" | "langgraph">("sequential");
  // What the server reported actually ran, which is not necessarily what was
  // requested — "auto" falls back silently, so the claim must come from the
  // response rather than from the dropdown.
  const [ran, setRan] = useState<{ engine: string; checkpointed: boolean } | null>(
    null,
  );

  useEffect(() => {
    donations
      .mine()
      .then((rows) => {
        setList(rows);
        if (rows.length > 0) setSelected(rows[0].id);
      })
      .catch((err) =>
        setError(err instanceof ApiRequestError ? err.message : "could not load"),
      );
  }, []);

  const loadEvents = useCallback(async (donationId: string) => {
    setBusy(true);
    try {
      setData(await workflow.events(donationId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "could not load events");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (selected) void loadEvents(selected);
  }, [selected, loadEvents]);

  async function runWorkflow() {
    if (!selected) return;
    setBusy(true);
    try {
      const result = await workflow.run(selected, undefined, engine);
      setRan({ engine: result.engine, checkpointed: result.checkpointed });
      if (result.warning) setError(result.warning);
      await loadEvents(selected);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "run failed");
    } finally {
      setBusy(false);
    }
  }

  async function resumeWorkflow() {
    if (!selected) return;
    setBusy(true);
    try {
      await workflow.resume(selected);
      await loadEvents(selected);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "resume failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <Card
        title="Workflow"
        subtitle="Real orchestrator events, read from the workflow_events table."
        actions={
          <div className="row">
            <select
              id="engine-select"
              value={engine}
              onChange={(e) =>
                setEngine(e.target.value as "sequential" | "langgraph")
              }
              disabled={busy}
            >
              <option value="sequential">sequential</option>
              <option value="langgraph">langgraph</option>
            </select>
            <button type="button" className="btn btn-sm" onClick={() => void runWorkflow()} disabled={busy || !selected}>
              Run
            </button>
            <button type="button" className="btn btn-sm" onClick={() => void resumeWorkflow()} disabled={busy || !selected}>
              Resume
            </button>
          </div>
        }
      >
        {list === null && <Spinner label="Loading donations" />}

        {list !== null && list.length === 0 && (
          <EmptyState title="No donations to trace" hint="Seed the database first." />
        )}

        {list !== null && list.length > 0 && (
          <label className="field">
            <span className="field-label">Donation</span>
            <select
              id="donation-select"
              value={selected ?? ""}
              onChange={(e) => setSelected(e.target.value)}
            >
              {list.map((donation) => (
                <option key={donation.id} value={donation.id}>
                  {donation.title} — {donation.status.toLowerCase()}
                </option>
              ))}
            </select>
          </label>
        )}

        {error && <ErrorBox message={error} />}

        {ran && (
          <div className="box box-info">
            <p>
              <strong>Ran on the {ran.engine} engine.</strong>{" "}
              {ran.checkpointed
                ? "Checkpointed after every node, so this run survives a restart."
                : "Not checkpointed: the same nodes and routers in plain Python, so a crash loses this run."}
            </p>
          </div>
        )}

        {data?.run && <RunSummary data={data} />}

        {busy && <Spinner label="Loading events" />}

        {data && data.events.length === 0 && !busy && (
          <EmptyState
            title="This donation has no workflow events"
            hint="Press Run to start the orchestrator."
          />
        )}

        {data && data.events.length > 0 && <Timeline events={data.events} />}

        {data && (
          <p className="small provenance-note">{data.note}</p>
        )}
      </Card>

      <BranchCatalogue />
    </div>
  );
}

function RunSummary({ data }: { data: WorkflowEventsResponse }) {
  const run = data.run!;
  const attempts = Object.entries(run.attempts ?? {});

  return (
    <div className="run-summary">
      <div className="run-row">
        <span className="run-label">Status</span>
        <StatusPill status={run.status} />
      </div>
      <div className="run-row">
        <span className="run-label">Thread</span>
        <code>{run.thread_id}</code>
      </div>
      {run.interrupt_reason && (
        <div className="run-row">
          <span className="run-label">Waiting on</span>
          <strong>{humanize(run.interrupt_reason)}</strong>
        </div>
      )}
      {attempts.length > 0 && (
        <div className="run-row">
          <span className="run-label">Attempts</span>
          <span>
            {attempts.map(([key, value]) => (
              <code key={key} className="attempt">
                {key}: {value}
              </code>
            ))}
          </span>
        </div>
      )}
      {run.last_error && (
        <div className="run-row">
          <span className="run-label">Last error</span>
          <span className="small">{run.last_error}</span>
        </div>
      )}
      <div className="run-row">
        <span className="run-label">Events</span>
        <span>{data.event_count}</span>
      </div>
    </div>
  );
}

function Timeline({ events }: { events: WorkflowEvent[] }) {
  return (
    <ol className="timeline">
      {events.map((event, index) => (
        <li key={`${event.node}-${event.created_at}-${index}`} className={`tl tl-${event.phase.toLowerCase()}`}>
          <div className="tl-marker" aria-hidden="true" />
          <div className="tl-body">
            <div className="tl-head">
              <strong>{humanize(event.node)}</strong>
              <span className={`phase phase-${event.phase.toLowerCase()}`}>
                {event.phase.toLowerCase()}
              </span>
              {event.duration_ms !== null && (
                <span className="small">{event.duration_ms} ms</span>
              )}
              <span className="tl-time">{formatDateTime(event.created_at)}</span>
            </div>

            {event.summary && <p className="tl-summary">{event.summary}</p>}

            {event.to_state && (
              <p className="small">
                → <code>{event.to_state}</code>
              </p>
            )}

            {event.payload && Object.keys(event.payload).length > 0 && (
              <details className="tl-payload">
                <summary>payload</summary>
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </details>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

/** The failure branches, read from the API rather than hardcoded here. */
function BranchCatalogue() {
  const [branches, setBranches] = useState<Array<Record<string, string>> | null>(null);
  const [note, setNote] = useState<string>("");

  useEffect(() => {
    workflow
      .branches()
      .then((data) => {
        setBranches(data.branches);
        setNote(data.note);
      })
      .catch(() => setBranches([]));
  }, []);

  if (branches === null) return null;
  if (branches.length === 0) return null;

  return (
    <Card title="Failure branches" subtitle="Every cycle has an explicit bound.">
      <table className="branches">
        <thead>
          <tr>
            <th>Branch</th>
            <th>Fires when</th>
            <th>Bound</th>
            <th>Demonstrates</th>
          </tr>
        </thead>
        <tbody>
          {branches.map((branch) => (
            <tr key={branch.name}>
              <td><code>{branch.name}</code></td>
              <td>{branch.fires_when}</td>
              <td>{branch.bound}</td>
              <td className="small">{branch.teaches}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">{note}</p>
    </Card>
  );
}
