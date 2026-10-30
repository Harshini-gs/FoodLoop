"""Branch tests. No database, no graph, no LangGraph import.

This file is the payoff of keeping the routers pure. Every branch in the
application is tested here with a hand-written dict, in milliseconds, and
these tests keep passing across a LangGraph upgrade because they never touch
it.

The loop-bound tests are the important ones: they prove each cycle terminates
by design rather than by a recursion limit.
"""

from app.workflows.routers import BRANCHES, route_after_confirmation
from app.workflows.routers import (
    route_after_extraction,
    route_after_handover,
    route_after_matching,
    route_after_pickup,
    route_after_reservation,
    route_after_safety,
    route_after_validation,
)
from app.workflows.state import (
    ATTEMPT_CANDIDATE,
    ATTEMPT_CLARIFICATION,
    FoodLoopState,
    bumped,
    initial_state,
    merge_attempts,
)


def state(**overrides: object) -> FoodLoopState:
    base = initial_state(donation_id="d1", run_id="r1")
    base.update(overrides)   # type: ignore[typeddict-item]
    return base


# ---------------------------------------------------------------------------
# extraction and the clarification loop
# ---------------------------------------------------------------------------

def test_complete_draft_proceeds() -> None:
    assert route_after_extraction(state(missing_fields=[])) == "validate_listing"


def test_missing_fields_ask_the_donor() -> None:
    result = route_after_extraction(state(missing_fields=["quantity_total"]))
    assert result == "request_clarification"


def test_clarification_loop_is_bounded() -> None:
    """After MAX_CLARIFICATION_ROUNDS the donation parks instead of looping.

    This is the test that proves the cycle terminates. If it fails, the graph
    can spin forever asking the same question.
    """
    from app.core.settings import get_settings

    cap = get_settings().max_clarification_rounds

    for rounds in range(cap):
        s = state(missing_fields=["quantity_total"], attempts={ATTEMPT_CLARIFICATION: rounds})
        assert route_after_extraction(s) == "request_clarification", (
            f"round {rounds} should still ask"
        )

    exhausted = state(
        missing_fields=["quantity_total"], attempts={ATTEMPT_CLARIFICATION: cap}
    )
    assert route_after_extraction(exhausted) == "pending_info"


def test_clarification_loop_cannot_be_reset_by_a_concurrent_update() -> None:
    """The attempts reducer takes the max, so a counter never goes backwards.

    Without this, two nodes updating state could reset the counter and turn a
    bounded loop into an infinite one.
    """
    merged = merge_attempts({ATTEMPT_CLARIFICATION: 2}, {ATTEMPT_CLARIFICATION: 0})
    assert merged[ATTEMPT_CLARIFICATION] == 2


def test_bumped_does_not_mutate_input() -> None:
    s = state(attempts={ATTEMPT_CANDIDATE: 1})
    result = bumped(s, ATTEMPT_CANDIDATE)
    assert result[ATTEMPT_CANDIDATE] == 2
    assert s["attempts"][ATTEMPT_CANDIDATE] == 1, "bumped() mutated shared state"


# ---------------------------------------------------------------------------
# the safety gate
# ---------------------------------------------------------------------------

def test_only_eligible_proceeds_to_matching() -> None:
    assert route_after_safety(state(safety_outcome="ELIGIBLE")) == "find_and_rank_recipients"


def test_needs_review_goes_to_a_human() -> None:
    assert route_after_safety(state(safety_outcome="NEEDS_REVIEW")) == "human_safety_review"


def test_rejected_is_terminal() -> None:
    assert route_after_safety(state(safety_outcome="REJECTED")) == "close_rejected"


def test_unknown_safety_outcome_does_not_proceed() -> None:
    """Fail closed. An unexpected value must not reach matching.

    If someone adds a fourth outcome and forgets this router, the donation
    stops rather than slipping through the gate.
    """
    for outcome in (None, "PENDING", "MAYBE", ""):
        assert route_after_safety(state(safety_outcome=outcome)) != "find_and_rank_recipients"


# ---------------------------------------------------------------------------
# matching and the reservation-conflict loop
# ---------------------------------------------------------------------------

def test_no_candidates_holds() -> None:
    assert route_after_matching(state(candidates=[])) == "no_recipient_hold"


def test_candidates_attempt_a_reservation() -> None:
    s = state(candidates=[{"recipient_name": "NGO"}], candidate_index=0)
    assert route_after_matching(s) == "reserve_quantity"


def test_exhausted_candidate_list_holds() -> None:
    s = state(candidates=[{"recipient_name": "NGO"}], candidate_index=1)
    assert route_after_matching(s) == "no_recipient_hold"


def test_successful_reservation_awaits_confirmation() -> None:
    assert route_after_reservation(state(reservation_id="res-1")) == "await_confirmation"


def test_conflict_tries_the_next_candidate() -> None:
    s = state(
        reservation_id=None,
        reservation_error="only 10 remaining",
        candidates=[{"n": 1}, {"n": 2}, {"n": 3}],
        candidate_index=1,
        attempts={ATTEMPT_CANDIDATE: 1},
    )
    assert route_after_reservation(s) == "reserve_quantity"


def test_candidate_loop_is_bounded() -> None:
    """After MAX_CANDIDATE_ATTEMPTS the donation holds rather than spinning
    through every NGO in the city."""
    from app.core.settings import get_settings

    cap = get_settings().max_candidate_attempts
    s = state(
        reservation_id=None,
        candidates=[{"n": i} for i in range(20)],
        candidate_index=cap,
        attempts={ATTEMPT_CANDIDATE: cap},
    )
    assert route_after_reservation(s) == "no_recipient_hold"


def test_candidate_loop_stops_when_the_list_runs_out_first() -> None:
    """Two independent bounds: the attempt cap AND the list length. Either one
    ending the loop is enough."""
    s = state(
        reservation_id=None,
        candidates=[{"n": 1}],
        candidate_index=1,
        attempts={ATTEMPT_CANDIDATE: 1},
    )
    assert route_after_reservation(s) == "no_recipient_hold"


# ---------------------------------------------------------------------------
# confirmation, pickup, handover
# ---------------------------------------------------------------------------

def test_still_waiting_stays_interrupted() -> None:
    s = state(needs_human="recipient_confirmation", reservation_id="res-1")
    assert route_after_confirmation(s) == "await_confirmation"


def test_confirmed_schedules_a_pickup() -> None:
    s = state(needs_human=None, reservation_id="res-1")
    assert route_after_confirmation(s) == "assign_pickup"


def test_declined_falls_back_to_the_candidate_loop() -> None:
    s = state(
        needs_human=None,
        reservation_id=None,
        candidates=[{"n": 1}, {"n": 2}],
        candidate_index=1,
        attempts={ATTEMPT_CANDIDATE: 1},
    )
    assert route_after_confirmation(s) == "reserve_quantity"


def test_pickup_failure_escalates() -> None:
    s = state(pickup_id=None, last_error="no volunteer with enough capacity")
    assert route_after_pickup(s) == "escalate_human"


def test_handover_waits_for_a_verified_delivery() -> None:
    """A collection alone does not close the donation. Only a delivery does,
    and only a human with a code can record one."""
    collected_only = state(collected_quantity="47.00", delivered_quantity=None)
    assert route_after_handover(collected_only) == "await_handover"

    delivered = state(collected_quantity="47.00", delivered_quantity="47.00")
    assert route_after_handover(delivered) == "update_impact"


def test_validation_failure_escalates() -> None:
    assert route_after_validation(state(last_error="bad listing")) == "escalate_human"
    assert route_after_validation(state(last_error=None)) == "safety_evaluate"


# ---------------------------------------------------------------------------
# the branch table
# ---------------------------------------------------------------------------

def test_every_branch_declares_a_bound() -> None:
    """Documentation that cannot rot: a new branch with no stated bound fails
    this test."""
    for branch in BRANCHES:
        assert branch["bound"], f"branch {branch['name']} declares no bound"
        assert branch["teaches"], f"branch {branch['name']} has no rationale"


def test_branch_table_references_real_routers() -> None:
    import app.workflows.routers as module

    for branch in BRANCHES:
        assert hasattr(module, branch["router"]), (
            f"branch {branch['name']} names a router that does not exist"
        )
