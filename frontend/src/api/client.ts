/**
 * API client.
 *
 * One place that knows how to talk to the backend, so error shapes and the
 * Authorization header are handled once. `ApiRequestError` carries the HTTP
 * status, which matters because 409 is a normal outcome here — it means
 * another recipient won the reservation race, not that something broke.
 */

import type {
  AvailableDonation,
  Candidate,
  Donation,
  DonationDraft,
  ImpactReport,
  MatchingResult,
  MetricDefinition,
  PickupInfo,
  PickupScheduled,
  Reservation,
  SafetyCheck,
  TokenResponse,
  WorkflowEventsResponse,
} from "./types";

const TOKEN_KEY = "foodloop.token";

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiRequestError";
  }

  /** 409 on a reserve call: someone else claimed the quantity first. */
  get isConflict(): boolean {
    return this.status === 409;
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    // Private browsing can throw on access. Treat it as "no token" rather
    // than crashing the app on load.
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token === null) localStorage.removeItem(TOKEN_KEY);
    else localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* non-fatal: the session simply will not persist across a reload */
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { idempotencyKey?: string } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");

  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);

  const response = await fetch(`/api/v1${path}`, { ...init, headers });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const body = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const err = body?.error;
    throw new ApiRequestError(
      response.status,
      err?.code ?? "http_error",
      err?.message ?? `${response.status} ${response.statusText}`,
      err?.details ?? {},
    );
  }

  return body as T;
}

// ---------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------

export const auth = {
  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  me: () =>
    request<{ id: string; email: string; full_name: string; role: string; org_id: string | null }>(
      "/auth/me",
    ),
};

// ---------------------------------------------------------------------------
// donations
// ---------------------------------------------------------------------------

export const donations = {
  /** Free text -> a draft. Creates nothing; the donor confirms. */
  extract: (rawDescription: string) =>
    request<DonationDraft>("/donations/extract", {
      method: "POST",
      body: JSON.stringify({ raw_description: rawDescription }),
    }),

  create: (payload: Record<string, unknown>, rawDescription?: string) => {
    const query = rawDescription
      ? `?raw_description=${encodeURIComponent(rawDescription)}`
      : "";
    return request<Donation>(`/donations${query}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  mine: () => request<Donation[]>("/donations/mine"),

  available: () => request<AvailableDonation[]>("/donations/available"),

  get: (id: string) => request<Donation>(`/donations/${id}`),

  evaluateSafety: (id: string) =>
    request<SafetyCheck>(`/donations/${id}/evaluate-safety`, { method: "POST" }),

  safety: (id: string) => request<SafetyCheck>(`/donations/${id}/safety`),

  cancel: (id: string, reason: string) =>
    request<Donation>(`/donations/${id}/cancel?reason=${encodeURIComponent(reason)}`, {
      method: "POST",
    }),
};

// ---------------------------------------------------------------------------
// matching and reservations
// ---------------------------------------------------------------------------

export const matching = {
  candidates: (donationId: string, persist = false) =>
    request<MatchingResult>(
      `/matching/${donationId}/candidates?persist=${persist}`,
    ),

  /**
   * Claim quantity. Throws ApiRequestError with isConflict on a lost race.
   * The idempotency key makes a retry safe — the same key returns the
   * original reservation rather than creating a second hold.
   */
  reserve: (donationId: string, quantity: string, idempotencyKey?: string) =>
    request<Reservation>("/matching/reserve", {
      method: "POST",
      body: JSON.stringify({ donation_id: donationId, quantity }),
      idempotencyKey: idempotencyKey ?? `${donationId}:${quantity}:${Date.now()}`,
    }),

  confirm: (reservationId: string) =>
    request<Reservation>(`/matching/reservations/${reservationId}/confirm`, {
      method: "POST",
    }),

  decline: (reservationId: string, reason = "declined by recipient") =>
    request<Reservation>(
      `/matching/reservations/${reservationId}/decline?reason=${encodeURIComponent(reason)}`,
      { method: "POST" },
    ),

  mine: () => request<Reservation[]>("/matching/reservations/mine"),
};

// ---------------------------------------------------------------------------
// handover
// ---------------------------------------------------------------------------

export const handover = {
  /** Returns the plaintext code ONCE. Only a hash is stored server-side. */
  assign: (reservationId: string) =>
    request<PickupScheduled>(`/handover/reservations/${reservationId}/assign`, {
      method: "POST",
    }),

  // `notes` comes back carrying the DELIVERY code, which the backend generates
  // at collection time. The recipient needs it to confirm receipt, so it must
  // be in the response type or the UI has no way to surface it.
  collect: (pickupId: string, code: string, quantity: string, notes?: string) =>
    request<{ id: string; quantity: string; occurred_at: string; notes: string | null }>(
      `/handover/pickups/${pickupId}/collect`,
      {
        method: "POST",
        body: JSON.stringify({ kind: "COLLECT", code, quantity, notes }),
        idempotencyKey: `collect:${pickupId}`,
      },
    ),

  deliver: (pickupId: string, code: string, quantity: string, notes?: string) =>
    request<{ id: string; quantity: string; occurred_at: string; notes: string | null }>(
      `/handover/pickups/${pickupId}/deliver`,
      {
        method: "POST",
        body: JSON.stringify({ kind: "DELIVER", code, quantity, notes }),
        idempotencyKey: `deliver:${pickupId}`,
      },
    ),

  // Both lookups return the pickup WITHOUT any code — see `PickupInfo`. They
  // exist so that either party can find a pickup assigned in an earlier
  // session; before this, the pickup id lived only in React state and closing
  // the tab stranded the handover.
  forDonation: (donationId: string) =>
    request<PickupInfo | null>(`/handover/donations/${donationId}/pickup`),

  forReservation: (reservationId: string) =>
    request<PickupInfo | null>(`/handover/reservations/${reservationId}/pickup`),
};

// ---------------------------------------------------------------------------
// workflow
// ---------------------------------------------------------------------------

export const workflow = {
  // `engine` is explicit rather than hidden behind "auto" because which
  // runner executed is a fact about the run, not an implementation detail:
  // only the langgraph path is checkpointed, and a resume guarantee that
  // silently did not apply would be worse than no guarantee at all.
  run: (
    donationId: string,
    rawDescription?: string,
    engine?: "auto" | "langgraph" | "sequential",
  ) => {
    const params = new URLSearchParams();
    if (rawDescription) params.set("raw_description", rawDescription);
    if (engine) params.set("engine", engine);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request<{ engine: string; checkpointed: boolean; state: Record<string, unknown>; warning?: string }>(
      `/workflow/${donationId}/run${query}`,
      { method: "POST" },
    );
  },

  resume: (donationId: string, clarificationAnswer?: string) => {
    const query = clarificationAnswer
      ? `?clarification_answer=${encodeURIComponent(clarificationAnswer)}`
      : "";
    return request<{ resumed_from: string[]; state: Record<string, unknown> }>(
      `/workflow/${donationId}/resume${query}`,
      { method: "POST" },
    );
  },

  /** Real rows from workflow_events. Nothing here is generated for display. */
  events: (donationId: string) =>
    request<WorkflowEventsResponse>(`/workflow/${donationId}/events`),

  branches: () =>
    request<{ branches: Array<Record<string, string>>; note: string }>(
      "/workflow/branches",
    ),
};

// ---------------------------------------------------------------------------
// impact
// ---------------------------------------------------------------------------

export const impact = {
  report: () => request<ImpactReport>("/impact/report"),
  definitions: () => request<MetricDefinition[]>("/impact/definitions"),
  narrative: () =>
    request<{ narrative: string; generated_from: string[]; note: string }>(
      "/impact/narrative",
    ),
};

// ---------------------------------------------------------------------------
// admin
// ---------------------------------------------------------------------------

export const admin = {
  safetyQueue: () => request<Array<Record<string, unknown>>>("/admin/safety-queue"),

  resolve: (donationId: string, approve: boolean, notes: string) =>
    request<Record<string, unknown>>(
      `/admin/safety-queue/${donationId}/resolve?approve=${approve}&notes=${encodeURIComponent(notes)}`,
      { method: "POST" },
    ),

  sweepExpired: () =>
    request<{ released: number }>("/matching/sweep-expired", { method: "POST" }),
};

export async function health(): Promise<{
  status: string;
  db: string;
  postgis: string | null;
  migration: string | null;
}> {
  const response = await fetch("/health");
  return response.json();
}

export type { Candidate };
