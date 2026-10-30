"""Branch functions. Pure, and that is the whole point.

Every routing decision in the graph is a function of state alone — no
database, no LLM, no clock. Three consequences:

  * they unit-test in microseconds with hand-written dicts, so "every branch
    is tested independently" costs almost nothing
  * a LangGraph version bump touches `graph.py` only; these keep working
  * a reviewer can read the entire control flow of the application in one
    short file

Every loop-back is bounded by a counter in `state["attempts"]`, compared
against a cap from settings. There is no recursion limit anywhere.
"""

from app.core.settings import get_settings
from app.workflows.state import (
    ATTEMPT_CANDIDATE,
    ATTEMPT_CLARIFICATION,
    FoodLoopState,
    attempts_for,
)


def route_after_extraction(state: FoodLoopState) -> str:
    """Draft complete? Proceed. Otherwise ask the donor — at most twice."""
    if not state.get("missing_fields"):
        return "validate_listing"

    settings = get_settings()
    rounds = attempts_for(state, ATTEMPT_CLARIFICATION)

    if rounds >= settings.max_clarification_rounds:
        # Out of rounds. Park it rather than looping, and say why.
        return "pending_info"

    return "request_clarification"


def route_after_validation(state: FoodLoopState) -> str:
    if state.get("last_error"):
        return "escalate_human"
    return "safety_evaluate"


def route_after_safety(state: FoodLoopState) -> str:
    """The gate. Only ELIGIBLE proceeds.

    NEEDS_REVIEW goes to a human and never auto-resolves — there is
    deliberately no retry, no timeout and no fallback path out of review. A
    system that eventually gives up waiting and proceeds would make the review
    decorative.
    """
    outcome = state.get("safety_outcome")
    if outcome == "ELIGIBLE":
        return "find_and_rank_recipients"
    if outcome == "NEEDS_REVIEW":
        return "human_safety_review"
    return "close_rejected"


def route_after_matching(state: FoodLoopState) -> str:
    """Any candidates? Try the best one. Otherwise hold."""
    candidates = state.get("candidates") or []
    if not candidates:
        return "no_recipient_hold"

    if state.get("candidate_index", 0) >= len(candidates):
        return "no_recipient_hold"

    return "reserve_quantity"


def route_after_reservation(state: FoodLoopState) -> str:
    """The reservation-conflict branch.

    Losing the race is a normal outcome, not an error: move to the next ranked
    candidate, up to MAX_CANDIDATE_ATTEMPTS. After that, hold the donation
    rather than spinning through every NGO in the city.
    """
    if state.get("reservation_id"):
        return "await_confirmation"

    settings = get_settings()
    tried = attempts_for(state, ATTEMPT_CANDIDATE)

    if tried >= settings.max_candidate_attempts:
        return "no_recipient_hold"

    candidates = state.get("candidates") or []
    if state.get("candidate_index", 0) >= len(candidates):
        return "no_recipient_hold"

    return "reserve_quantity"       # the bounded loop back


def route_after_confirmation(state: FoodLoopState) -> str:
    """Accepted -> schedule. Declined or timed out -> next candidate."""
    if state.get("needs_human"):
        return "await_confirmation"      # still waiting; the graph interrupts

    if state.get("reservation_id"):
        return "assign_pickup"

    return route_after_reservation(state)


def route_after_pickup(state: FoodLoopState) -> str:
    """A pickup that cannot be assigned is escalated, not silently dropped."""
    if state.get("pickup_id"):
        return "await_handover"
    if state.get("last_error"):
        return "escalate_human"
    return "escalate_human"


def route_after_handover(state: FoodLoopState) -> str:
    """Handover is recorded outside the graph, by an authenticated human.

    The graph resumes once a delivery event exists. If only a collection
    happened, it keeps waiting rather than inventing a delivery.
    """
    if state.get("delivered_quantity") is not None:
        return "update_impact"
    return "await_handover"


def is_terminal(state: FoodLoopState) -> bool:
    return state.get("terminal_reason") is not None


def should_interrupt(state: FoodLoopState) -> bool:
    return state.get("needs_human") is not None


# ---------------------------------------------------------------------------
# A table of every branch, for documentation and for the tests to iterate.
# Keeping this beside the functions means a new branch without a bound is
# visible in review.
# ---------------------------------------------------------------------------

BRANCHES: list[dict[str, str]] = [
    {
        "name": "clarification",
        "router": "route_after_extraction",
        "fires_when": "the extracted draft is missing a mandatory field",
        "bound": "MAX_CLARIFICATION_ROUNDS (2), then PENDING_INFO",
        "teaches": "bounded cycles with an explicit counter",
    },
    {
        "name": "safety_review",
        "router": "route_after_safety",
        "fires_when": "the rules engine returns NEEDS_REVIEW",
        "bound": "none — never auto-resolves, by design",
        "teaches": "when a system must stop and wait for a person",
    },
    {
        "name": "safety_rejection",
        "router": "route_after_safety",
        "fires_when": "a hard safety rule failed",
        "bound": "terminal",
        "teaches": "a decision no agent may appeal",
    },
    {
        "name": "no_recipient",
        "router": "route_after_matching",
        "fires_when": "no eligible recipient was found after widening",
        "bound": "MAX_SEARCH_WIDENINGS (2), then hold",
        "teaches": "degrading gracefully instead of failing",
    },
    {
        "name": "reservation_conflict",
        "router": "route_after_reservation",
        "fires_when": "another recipient won the quantity first",
        "bound": "MAX_CANDIDATE_ATTEMPTS (3), then hold",
        "teaches": "recovering from a lost race",
    },
    {
        "name": "confirmation_timeout",
        "router": "route_after_confirmation",
        "fires_when": "the recipient did not answer before the hold expired",
        "bound": "shares the candidate budget",
        "teaches": "durable timers that survive a restart",
    },
    {
        "name": "pickup_infeasible",
        "router": "route_after_pickup",
        "fires_when": "no volunteer has enough vehicle capacity",
        "bound": "escalates to a human immediately",
        "teaches": "explaining infeasibility rather than failing silently",
    },
]
