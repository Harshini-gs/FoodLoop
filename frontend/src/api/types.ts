/**
 * Types mirroring the backend's Pydantic schemas.
 *
 * Kept hand-written rather than generated from OpenAPI: the generated client
 * would be another build step to maintain, and this file is small enough that
 * drift is caught the first time a field is missing.
 */

export type UserRole = "DONOR" | "RECIPIENT" | "VOLUNTEER" | "ADMIN";

export type FoodCategory =
  | "COOKED_VEG"
  | "COOKED_NONVEG"
  | "BAKERY"
  | "PRODUCE"
  | "DAIRY"
  | "PACKAGED_DRY";

export type QuantityUnit = "MEALS" | "KG" | "BOXES" | "LITRES";

/** Where a number came from. Shown in the UI, never hidden. */
export type DataSource = "REAL" | "CACHED" | "SIMULATED" | "ESTIMATED";

export type SafetyStatus = "PENDING" | "ELIGIBLE" | "NEEDS_REVIEW" | "REJECTED";

export type DonationStatus =
  | "DRAFT"
  | "PENDING_INFO"
  | "SAFETY_REVIEW"
  | "ELIGIBLE"
  | "PARTIALLY_RESERVED"
  | "FULLY_RESERVED"
  | "IN_TRANSIT"
  | "COMPLETED"
  | "REJECTED"
  | "CANCELLED"
  | "EXPIRED"
  | "NEEDS_MANUAL_REVIEW";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  role: UserRole;
  user_id: string;
  org_id: string | null;
}

export interface Donation {
  id: string;
  donor_org_id: string;
  title: string;
  food_category: FoodCategory;
  unit: QuantityUnit;
  quantity_total: string;
  quantity_reserved: string;
  quantity_collected: string;
  quantity_delivered: string;
  prepared_at: string | null;
  pickup_window_start: string;
  pickup_window_end: string;
  status: DonationStatus;
  safety_status: SafetyStatus;
  missing_fields: string[] | null;
  data_source: DataSource;
  created_at: string;
}

/** The extraction result. Every field optional — that is the contract. */
export interface DonationDraft {
  title: string | null;
  food_category: FoodCategory | null;
  unit: QuantityUnit | null;
  quantity_total: string | null;
  prepared_at: string | null;
  pickup_window_start: string | null;
  pickup_window_end: string | null;
  pickup_address_text: string | null;
  storage_notes: string | null;
  kept_refrigerated: boolean | null;
  allergen_notes: string | null;
  missing_fields: string[];
  ambiguities: string[];
  unit_normalizations: string[];
  clarification_question: string | null;
}

export interface SafetyRuleResult {
  rule_id: string;
  severity: "hard" | "soft";
  /** null means the rule could not be evaluated — evidence was missing. */
  passed: boolean | null;
  reason: string;
  observed: string | null;
  limit: string | null;
}

export interface SafetyCheck {
  id: string;
  donation_id: string;
  outcome: SafetyStatus;
  per_rule: SafetyRuleResult[];
  unevaluated: string[] | null;
  created_at: string;
  rule_version: string | null;
  /** Shown in the UI so a demo policy is never mistaken for a verified one. */
  policy_status: string | null;
}

export interface ScoreComponent {
  name: string;
  value: number;
  weight: number;
  raw: Record<string, unknown>;
  explanation: string;
}

export interface Candidate {
  recipient_org_id: string;
  recipient_name: string;
  rank: number;
  score: number;
  components: ScoreComponent[];
  distance_meters: number | null;
  allocatable_quantity: string;
}

export interface ExcludedCandidate {
  recipient_org_id: string;
  recipient_name: string;
  reason: string;
}

export interface MatchingResult {
  donation_id: string;
  search_radius_meters: number;
  widenings_applied: number;
  candidates: Candidate[];
  excluded: ExcludedCandidate[];
  evaluated_at: string;
}

export interface AvailableDonation {
  id: string;
  title: string;
  food_category: FoodCategory;
  unit: QuantityUnit;
  quantity_total: string;
  quantity_reserved: string;
  pickup_window_start: string;
  pickup_window_end: string;
  status: DonationStatus;
  safety_status: SafetyStatus;
  data_source: DataSource;
  donor_name: string;
  /** Area only. The exact address is released with the pickup assignment. */
  donor_area: string | null;
}

export interface Reservation {
  id: string;
  donation_id: string;
  recipient_org_id: string;
  quantity: string;
  status: "HELD" | "CONFIRMED" | "CONSUMED" | "RELEASED";
  expires_at: string;
  created_at: string;
}

/**
 * A pickup as it can be read back at any time. Mirrors the backend's
 * `PickupOut`, which carries no handover code: the codes exist as hashes only,
 * so they are genuinely unrecoverable rather than merely hidden from the UI.
 */
export interface PickupInfo {
  id: string;
  reservation_id: string;
  volunteer_user_id: string | null;
  status: string;
  scheduled_for: string | null;
  estimated_distance_km: string | null;
  estimated_duration_min: number | null;
  data_source: DataSource;
  reassignment_count: number;
}

/** Only the assignment response carries the plaintext code, and only once. */
export interface PickupScheduled extends PickupInfo {
  /** Returned exactly once. Only a hash is stored server-side. */
  handover_code: string;
}

export type EventPhase = "ENTER" | "EXIT" | "ERROR" | "RETRY" | "INTERRUPT";

export interface WorkflowEvent {
  node: string;
  phase: EventPhase;
  from_state: string | null;
  to_state: string | null;
  summary: string | null;
  payload: Record<string, unknown> | null;
  attempt: number;
  duration_ms: number | null;
  created_at: string;
}

export interface WorkflowRunInfo {
  thread_id: string;
  status: string;
  current_node: string | null;
  interrupt_reason: string | null;
  attempts: Record<string, number> | null;
  last_error: string | null;
}

export interface WorkflowEventsResponse {
  donation_id: string;
  run: WorkflowRunInfo | null;
  events: WorkflowEvent[];
  event_count: number;
  note: string;
}

export interface MetricDefinition {
  key: string;
  label: string;
  formula: string;
  denominator: string | null;
  unit: string;
  inclusion_criteria: string;
  source_tables: string[];
  notes: string | null;
}

export interface Metric {
  key: string;
  label: string;
  /** null when not computable — a zero denominator is not a zero rate. */
  value: string | null;
  unit: string;
  provenance: DataSource;
  definition: MetricDefinition;
  sample_size: number;
  caveat: string | null;
}

export interface ImpactReport {
  generated_at: string;
  metrics: Metric[];
  data_coverage: string;
  limitations: string[];
  contains_simulated_data: boolean;
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}
