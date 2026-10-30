/**
 * Shared presentational pieces.
 *
 * `ProvenanceTag` is the one that matters. Every number drawn from a
 * simulated provider renders with it, so the UI cannot show an estimate as
 * though it were measured. That mirrors the `data_source` column, which is
 * NOT NULL with no default server-side.
 */

import type { ReactNode } from "react";

import type { DataSource, SafetyStatus } from "../api/types";

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="spinner" role="status" aria-live="polite">
      <span className="spinner-dot" />
      {label}
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="box box-error" role="alert">
      <strong>Something went wrong</strong>
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="box box-empty">
      <strong>{title}</strong>
      {hint && <p>{hint}</p>}
    </div>
  );
}

/** Says where a number came from. Never omitted on simulated data. */
export function ProvenanceTag({ source }: { source: DataSource }) {
  const text: Record<DataSource, string> = {
    REAL: "real",
    CACHED: "cached",
    SIMULATED: "simulated",
    ESTIMATED: "estimated",
  };
  return (
    <span className={`tag tag-${source.toLowerCase()}`} title={`data_source = ${source}`}>
      {text[source]}
    </span>
  );
}

export function SafetyBadge({ status }: { status: SafetyStatus }) {
  const label: Record<SafetyStatus, string> = {
    PENDING: "not evaluated",
    ELIGIBLE: "cleared",
    NEEDS_REVIEW: "needs human review",
    REJECTED: "rejected",
  };
  return (
    <span className={`badge badge-${status.toLowerCase()}`}>{label[status]}</span>
  );
}

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status.toLowerCase()}`}>{humanize(status)}</span>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}

export function Card({
  title,
  subtitle,
  actions,
  children,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="card-sub">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

/** A banner for the illustrative-demo safety policy. Shown, not buried. */
export function PolicyNotice({ policyStatus }: { policyStatus: string | null }) {
  if (!policyStatus) return null;
  const isDemo = policyStatus.toUpperCase().includes("DEMO");
  if (!isDemo) return <p className="policy-note">Policy: {policyStatus}</p>;
  return (
    <div className="box box-warn">
      <strong>Illustrative demo policy</strong>
      <p>
        The thresholds behind this decision are placeholders, not verified
        against any food-safety authority.
      </p>
      <code>{policyStatus}</code>
    </div>
  );
}

export function humanize(value: string): string {
  return value
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/^\w/, (c) => c.toUpperCase());
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatTimeOnly(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatQuantity(value: string | null, unit?: string): string {
  if (value === null) return "—";
  const num = Number(value);
  const clean = Number.isInteger(num) ? String(num) : num.toFixed(2);
  return unit ? `${clean} ${unit.toLowerCase()}` : clean;
}

export function formatDistance(meters: number | null): string {
  if (meters === null) return "—";
  return meters < 1000 ? `${Math.round(meters)} m` : `${(meters / 1000).toFixed(1)} km`;
}
