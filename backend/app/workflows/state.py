"""The typed shared state.

Every field is explicit and meaningful — no catch-all `context: dict`. If
something is worth carrying between nodes it is worth naming, because an
untyped bag is how workflows become unreadable.

`attempts` is where every loop bound lives. Each cycle in the graph increments
a counter here and compares it against a cap from settings. There are no
recursion limits anywhere in this project: a recursion limit is a crash, not a
design, and it tells you nothing about which loop ran away.
"""

from typing import Annotated, Any, Literal, TypedDict

# --- attempt counter keys. Named constants, so a typo is not a silent reset.
ATTEMPT_CLARIFICATION = "clarification"
ATTEMPT_CANDIDATE = "candidate"
ATTEMPT_SEARCH_WIDENING = "search_widening"


def merge_attempts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Reducer for the attempts dict.

    LangGraph merges state updates from each node. Without a reducer, two
    nodes updating `attempts` would clobber each other and a counter could
    silently reset — which would turn a bounded loop into an infinite one.
    Taking the max is conservative: a counter never goes backwards.
    """
    merged = dict(left)
    for key, value in right.items():
        merged[key] = max(merged.get(key, 0), value)
    return merged


class FoodLoopState(TypedDict, total=False):
    """State carried through the graph.

    Everything here must be JSON-serializable: it is written to a Postgres
    checkpoint after every node, and a run that cannot be serialized cannot be
    resumed. That is why ids are strings rather than UUID objects, and
    timestamps are ISO strings.
    """

    # --- identity ---
    donation_id: str
    run_id: str
    thread_id: str

    # --- extraction (set by extract_fields) ---
    raw_description: str | None
    draft: dict[str, Any] | None
    missing_fields: list[str]
    clarification_question: str | None
    clarification_answer: str | None

    # --- safety (set by safety_evaluate) ---
    safety_outcome: str | None          # ELIGIBLE | NEEDS_REVIEW | REJECTED
    safety_summary: str | None
    safety_check_id: str | None

    # --- matching (set by find_and_rank_recipients) ---
    candidates: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    candidate_index: int                # which ranked candidate we are trying
    search_radius_m: int

    # --- reservation (set by reserve_quantity) ---
    reservation_id: str | None
    reserved_quantity: str | None
    reservation_error: str | None

    # --- pickup and handover ---
    pickup_id: str | None
    collected_quantity: str | None
    delivered_quantity: str | None

    # --- impact ---
    impact_summary: str | None

    # --- control flow ---
    attempts: Annotated[dict[str, int], merge_attempts]
    needs_human: str | None             # interrupt reason, None when running
    terminal_reason: str | None
    last_error: str | None
    node_history: list[str]


def initial_state(
    *, donation_id: str, run_id: str, raw_description: str | None = None
) -> FoodLoopState:
    return FoodLoopState(
        donation_id=donation_id,
        run_id=run_id,
        thread_id=f"donation:{donation_id}",
        raw_description=raw_description,
        draft=None,
        missing_fields=[],
        clarification_question=None,
        clarification_answer=None,
        safety_outcome=None,
        safety_summary=None,
        safety_check_id=None,
        candidates=[],
        excluded=[],
        candidate_index=0,
        search_radius_m=0,
        reservation_id=None,
        reserved_quantity=None,
        reservation_error=None,
        pickup_id=None,
        collected_quantity=None,
        delivered_quantity=None,
        impact_summary=None,
        attempts={},
        needs_human=None,
        terminal_reason=None,
        last_error=None,
        node_history=[],
    )


def attempts_for(state: FoodLoopState, key: str) -> int:
    return (state.get("attempts") or {}).get(key, 0)


def bumped(state: FoodLoopState, key: str) -> dict[str, int]:
    """Return an attempts dict with `key` incremented.

    Returns a NEW dict rather than mutating: LangGraph state updates are
    merged, and mutating shared state in place produces bugs that only appear
    on resume.
    """
    current = dict(state.get("attempts") or {})
    current[key] = current.get(key, 0) + 1
    return current


NodeName = Literal[
    "ingest_donation",
    "extract_fields",
    "validate_listing",
    "safety_evaluate",
    "human_safety_review",
    "find_and_rank_recipients",
    "reserve_quantity",
    "await_confirmation",
    "assign_pickup",
    "update_impact",
    "close_workflow",
    "close_rejected",
    "no_recipient_hold",
    "escalate_human",
]
