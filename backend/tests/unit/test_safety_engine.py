"""Safety engine tests. No database, no clock, no network.

The engine takes a flat dict of facts, so every case below is a hand-written
dictionary. That is the payoff of keeping the checks pure.

The most important tests here are the ones asserting that missing evidence
cannot produce ELIGIBLE, and that a hard failure cannot be overridden.
"""

from datetime import UTC, datetime, time, timedelta

import pytest

from app.models.enums import SafetyStatus
from app.services.safety.checks import RuleResult, UnknownCheckType, _run_check
from app.services.safety.engine import (
    RulesetError,
    SafetyDecision,
    evaluate,
    get_ruleset,
    load_ruleset,
)

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)


def facts(**overrides: object) -> dict[str, object]:
    """A donation that passes every rule, unless overridden."""
    base: dict[str, object] = {
        "food_category": "COOKED_VEG",
        "unit": "MEALS",
        "quantity_total": 50,
        "prepared_at": NOW - timedelta(hours=1),
        "pickup_window_start": NOW + timedelta(hours=1),
        "pickup_window_end": NOW + timedelta(hours=2),
        "storage_notes": "Held in a covered warming counter since cooking.",
        "kept_refrigerated": False,
        "allergen_notes": "Contains dairy and wheat.",
        "donor_org_is_verified": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ruleset loading
# ---------------------------------------------------------------------------

def test_demo_ruleset_loads() -> None:
    ruleset = load_ruleset("demo_v1")
    assert ruleset.version == "demo_v1"
    assert ruleset.rules
    assert "ILLUSTRATIVE_DEMO" in ruleset.policy_status


def test_ruleset_without_policy_status_is_refused(tmp_path, monkeypatch) -> None:
    """The load-bearing refusal: a ruleset with no policy status cannot load.

    This is what stops a demo policy being mistaken for a verified one. If
    this test ever fails because someone added a default, that is a safety
    regression, not a test problem.
    """
    import app.services.safety.engine as engine_module

    bad = tmp_path / "nostatus.yaml"
    bad.write_text(
        "version: nostatus\n"
        "rules:\n"
        "  - id: r1\n"
        "    severity: hard\n"
        "    check: {type: field_present, field: quantity_total}\n"
    )
    monkeypatch.setattr(engine_module, "RULESET_DIR", tmp_path)

    with pytest.raises(RulesetError, match="policy_status"):
        load_ruleset("nostatus")


def test_missing_ruleset_names_what_is_available() -> None:
    with pytest.raises(RulesetError, match="not found"):
        load_ruleset("does_not_exist_v9")


# ---------------------------------------------------------------------------
# the three outcomes
# ---------------------------------------------------------------------------

def test_clean_donation_is_eligible() -> None:
    decision = evaluate(facts(), now=NOW)
    assert decision.outcome == SafetyStatus.ELIGIBLE
    assert decision.unevaluated == ()


def test_missing_preparation_time_never_passes() -> None:
    """THE critical test. Cooked food with no prepared_at cannot be ELIGIBLE.

    "We don't know when this was cooked" must never resolve to "this is safe".
    """
    decision = evaluate(facts(prepared_at=None), now=NOW)
    assert decision.outcome == SafetyStatus.NEEDS_REVIEW
    assert "cooked_safe_window" in decision.unevaluated


def test_stale_cooked_food_is_rejected() -> None:
    """Non-veg prepared 9 hours ago against a 2-hour placeholder limit."""
    decision = evaluate(
        facts(
            food_category="COOKED_NONVEG",
            prepared_at=NOW - timedelta(hours=9),
            kept_refrigerated=True,
        ),
        now=NOW,
    )
    assert decision.outcome == SafetyStatus.REJECTED
    assert any(r.rule_id == "cooked_safe_window" for r in decision.hard_failures)


def test_unverified_donor_needs_review() -> None:
    decision = evaluate(facts(donor_org_is_verified=False), now=NOW)
    assert decision.outcome == SafetyStatus.NEEDS_REVIEW


def test_hard_failure_beats_missing_evidence() -> None:
    """Resolution order matters: a demonstrable violation is a rejection even
    when something else is unknown. Otherwise an absent field could downgrade
    a rejection into a review."""
    decision = evaluate(
        facts(
            food_category="COOKED_NONVEG",
            prepared_at=NOW - timedelta(hours=9),
            kept_refrigerated=None,     # unevaluated
            storage_notes=None,         # unevaluated
        ),
        now=NOW,
    )
    assert decision.outcome == SafetyStatus.REJECTED


def test_past_pickup_window_is_rejected() -> None:
    decision = evaluate(
        facts(
            pickup_window_start=NOW - timedelta(hours=3),
            pickup_window_end=NOW - timedelta(hours=2),
        ),
        now=NOW,
    )
    assert decision.outcome == SafetyStatus.REJECTED


def test_dairy_without_refrigeration_is_rejected() -> None:
    decision = evaluate(
        facts(food_category="DAIRY", kept_refrigerated=False, prepared_at=None),
        now=NOW,
    )
    assert decision.outcome == SafetyStatus.REJECTED


def test_dairy_with_unknown_refrigeration_needs_review() -> None:
    """Contrast with the test above: False is a violation, None is a question."""
    decision = evaluate(
        facts(food_category="DAIRY", kept_refrigerated=None, prepared_at=None),
        now=NOW,
    )
    assert decision.outcome == SafetyStatus.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# immutability — the structural guarantee
# ---------------------------------------------------------------------------

def test_decision_cannot_be_rewritten() -> None:
    """An agent holding a decision cannot change the verdict.

    The frozen dataclass is the first line of defence; the database trigger is
    the second. Both exist because one of them might have a bug.
    """
    decision = evaluate(facts(prepared_at=None), now=NOW)
    assert decision.outcome == SafetyStatus.NEEDS_REVIEW

    with pytest.raises((AttributeError, TypeError)):
        decision.outcome = SafetyStatus.ELIGIBLE   # type: ignore[misc]

    with pytest.raises((AttributeError, TypeError)):
        decision.results[0].passed = True          # type: ignore[misc]


def test_decision_summary_names_the_failing_rule() -> None:
    decision = evaluate(
        facts(food_category="COOKED_NONVEG", prepared_at=NOW - timedelta(hours=9)),
        now=NOW,
    )
    assert "cooked_safe_window" in decision.summary()


# ---------------------------------------------------------------------------
# individual check types
# ---------------------------------------------------------------------------

def test_unknown_check_type_raises_rather_than_passing() -> None:
    """A typo in a ruleset must fail loudly. Silently skipping an unknown
    check would mean a rule that looks enforced but is not."""
    with pytest.raises(UnknownCheckType):
        _run_check("r", "hard", {}, {"type": "not_a_real_check"}, NOW, _tz())


def test_window_overlap_handles_midnight_wrap() -> None:
    """Sahaya Night Shelter opens 18:00 and closes 02:00.

    A naive `opens <= t <= closes` comparison excludes it from every late
    pickup, which is why the seed data contains this fixture.
    """
    tz = _tz()
    # 23:30-00:30 IST on 2026-10-27  ->  18:00 UTC is 23:30 IST
    start = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
    end = datetime(2026, 10, 27, 19, 0, tzinfo=UTC)

    result = _run_check(
        "within_operating_hours", "soft",
        {
            "pickup_window_start": start,
            "pickup_window_end": end,
            "opens_at": time(18, 0),
            "closes_at": time(2, 0),
        },
        {
            "type": "window_overlaps_hours",
            "window_start_field": "pickup_window_start",
            "window_end_field": "pickup_window_end",
            "opens_field": "opens_at",
            "closes_field": "closes_at",
        },
        NOW, tz,
    )
    assert result.passed is True, result.reason


def test_window_overlap_excludes_a_closed_recipient() -> None:
    tz = _tz()
    start = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)   # 23:30 IST
    end = datetime(2026, 10, 27, 19, 0, tzinfo=UTC)     # 00:30 IST

    result = _run_check(
        "within_operating_hours", "soft",
        {
            "pickup_window_start": start,
            "pickup_window_end": end,
            "opens_at": time(9, 0),
            "closes_at": time(18, 0),
        },
        {
            "type": "window_overlaps_hours",
            "window_start_field": "pickup_window_start",
            "window_end_field": "pickup_window_end",
            "opens_field": "opens_at",
            "closes_field": "closes_at",
        },
        NOW, tz,
    )
    assert result.passed is False, result.reason


def test_category_in_list() -> None:
    tz = _tz()
    config = {
        "type": "category_in_list",
        "donation_field": "food_category",
        "recipient_field": "accepted_categories",
    }
    accepted = _run_check(
        "category_accepted", "hard",
        {"food_category": "COOKED_VEG", "accepted_categories": ["COOKED_VEG", "BAKERY"]},
        config, NOW, tz,
    )
    assert accepted.passed is True

    refused = _run_check(
        "category_accepted", "hard",
        {"food_category": "DAIRY", "accepted_categories": ["COOKED_VEG", "BAKERY"]},
        config, NOW, tz,
    )
    assert refused.passed is False


def test_conditional_flag_passes_vacuously_when_condition_absent() -> None:
    """Non-veg held for 20 minutes does not need refrigeration under the demo
    policy, and the result says so rather than being silently skipped."""
    tz = _tz()
    result = _run_check(
        "nonveg_requires_refrigeration", "hard",
        {
            "prepared_at": NOW,
            "pickup_window_end": NOW + timedelta(minutes=20),
            "kept_refrigerated": None,
        },
        {
            "type": "conditional_flag_required",
            "flag_field": "kept_refrigerated",
            "when": {
                "type": "hours_between_at_least",
                "start_field": "prepared_at",
                "end_field": "pickup_window_end",
                "min_hours": 1.0,
            },
        },
        NOW, tz,
    )
    assert result.passed is True
    assert "not applicable" in result.reason


def test_rule_result_is_frozen() -> None:
    result = RuleResult(rule_id="r", severity="hard", passed=False, reason="x")
    with pytest.raises((AttributeError, TypeError)):
        result.passed = True   # type: ignore[misc]


def _tz():
    from zoneinfo import ZoneInfo

    from app.core.settings import get_settings

    return ZoneInfo(get_settings().default_timezone)
