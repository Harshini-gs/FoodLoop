"""The individual check types, as pure functions.

Every check takes (facts, config) and returns a RuleResult. No database, no
clock beyond an injected `now`, no I/O. That is what makes them trivially
unit-testable and what keeps the safety decision reproducible: given the same
facts and the same ruleset, the outcome is always identical.

`passed` is deliberately tri-state:
    True   the rule was evaluated and satisfied
    False  the rule was evaluated and violated
    None   the rule COULD NOT be evaluated — evidence was missing

That third state is the whole point. A system that collapses None into True
treats "we don't know when this was cooked" as "this was cooked recently",
which is exactly the failure mode this project exists to avoid.
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RuleResult:
    """Frozen on purpose: once a rule has been evaluated, nothing downstream
    may rewrite the verdict. An LLM holding this object cannot flip `passed`."""

    rule_id: str
    severity: str                      # "hard" | "soft"
    passed: bool | None                # None = unevaluated (missing evidence)
    reason: str
    observed: str | None = None
    limit: str | None = None
    missing: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "passed": self.passed,
            "reason": self.reason,
            "observed": self.observed,
            "limit": self.limit,
            "missing": list(self.missing),
        }


class UnknownCheckType(Exception):
    """A ruleset referenced a check type the engine does not implement."""


def _hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def _as_local_time(moment: datetime, tz: ZoneInfo) -> time:
    """Convert an aware UTC timestamp to local wall-clock time.

    Operating hours are stored as naive `time` values meaning local wall clock
    ("we open at 9am"), so any comparison against a timestamptz has to come
    through here first.
    """
    return moment.astimezone(tz).time()


def _window_overlaps(
    start: datetime, end: datetime, opens: time, closes: time, tz: ZoneInfo
) -> bool:
    """Does [start, end] overlap the daily opening hours?

    Handles the midnight wrap: an organization open 18:00-02:00 has
    `closes < opens`, and its open interval is [18:00, 24:00) ∪ [00:00, 02:00].
    Getting this wrong silently excludes every night shelter, which is why the
    seed data includes one.
    """
    start_local = _as_local_time(start, tz)
    end_local = _as_local_time(end, tz)

    def is_open(t: time) -> bool:
        if opens <= closes:
            return opens <= t <= closes
        return t >= opens or t <= closes   # wraps past midnight

    if is_open(start_local) or is_open(end_local):
        return True

    # A window can also fully contain the opening hours (e.g. a 24-hour
    # pickup window over a 9-5 recipient), in which case neither endpoint is
    # itself inside the hours.
    if _hours_between(start, end) >= 24:
        return True
    if start_local <= end_local:
        return start_local <= opens <= end_local
    return opens >= start_local or opens <= end_local


# ---------------------------------------------------------------------------
# Check implementations. Each one returns a RuleResult.
# ---------------------------------------------------------------------------

def check_field_present(rule_id: str, severity: str, facts: dict[str, Any],
                        config: dict[str, Any], now: datetime, tz: ZoneInfo) -> RuleResult:
    field_name = config["field"]
    value = facts.get(field_name)
    present = value is not None and value != ""
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=present,
        reason=f"{field_name} is {'present' if present else 'absent'}",
        observed=str(value) if present else None,
    )


def check_flag_is_true(rule_id: str, severity: str, facts: dict[str, Any],
                       config: dict[str, Any], now: datetime, tz: ZoneInfo) -> RuleResult:
    field_name = config.get("field") or config["recipient_field"]
    value = facts.get(field_name)
    if value is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"{field_name} was not recorded, so this cannot be evaluated",
            missing=(field_name,),
        )
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=bool(value),
        reason=f"{field_name} is {bool(value)}",
        observed=str(bool(value)), limit="must be true",
    )


def check_timestamp_after_now(rule_id: str, severity: str, facts: dict[str, Any],
                              config: dict[str, Any], now: datetime,
                              tz: ZoneInfo) -> RuleResult:
    field_name = config["field"]
    moment = facts.get(field_name)
    if moment is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"{field_name} is missing", missing=(field_name,),
        )
    ok = moment > now
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"{field_name} is {'in the future' if ok else 'already past'}",
        observed=moment.astimezone(tz).isoformat(),
        limit=f"must be after {now.astimezone(tz).isoformat()}",
    )


def check_timestamp_order(rule_id: str, severity: str, facts: dict[str, Any],
                          config: dict[str, Any], now: datetime,
                          tz: ZoneInfo) -> RuleResult:
    earlier_name, later_name = config["earlier"], config["later"]
    earlier, later = facts.get(earlier_name), facts.get(later_name)
    missing = tuple(n for n, v in ((earlier_name, earlier), (later_name, later)) if v is None)
    if missing:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"cannot compare: {', '.join(missing)} missing", missing=missing,
        )
    ok = earlier < later
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"{earlier_name} is {'before' if ok else 'not before'} {later_name}",
        observed=f"{earlier.isoformat()} -> {later.isoformat()}",
    )


def check_numeric_min(rule_id: str, severity: str, facts: dict[str, Any],
                      config: dict[str, Any], now: datetime, tz: ZoneInfo) -> RuleResult:
    field_name = config["field"]
    value = facts.get(field_name)
    if value is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"{field_name} is missing", missing=(field_name,),
        )
    threshold = Decimal(str(config["min_exclusive"]))
    ok = Decimal(str(value)) > threshold
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"{field_name} is {'above' if ok else 'not above'} {threshold}",
        observed=str(value), limit=f"> {threshold}",
    )


def check_text_min_length(rule_id: str, severity: str, facts: dict[str, Any],
                          config: dict[str, Any], now: datetime, tz: ZoneInfo) -> RuleResult:
    field_name = config["field"]
    value = facts.get(field_name)
    if value is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"{field_name} is missing", missing=(field_name,),
        )
    minimum = int(config["min_length"])
    ok = len(str(value).strip()) >= minimum
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"{field_name} is {len(str(value).strip())} characters",
        observed=f"{len(str(value).strip())} chars", limit=f">= {minimum} chars",
    )


def _resolve_hours_limit(config_value: Any, category: str | None) -> float | None:
    """`max_hours` is either a number or a per-category mapping."""
    if isinstance(config_value, dict):
        if category is None or category not in config_value:
            return None
        return float(config_value[category])
    return float(config_value)


def check_hours_between_at_most(rule_id: str, severity: str, facts: dict[str, Any],
                                config: dict[str, Any], now: datetime,
                                tz: ZoneInfo) -> RuleResult:
    start_name, end_name = config["start_field"], config["end_field"]
    start, end = facts.get(start_name), facts.get(end_name)
    missing = tuple(n for n, v in ((start_name, start), (end_name, end)) if v is None)
    if missing:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"cannot measure elapsed time: {', '.join(missing)} missing",
            missing=missing,
        )

    limit = _resolve_hours_limit(config["max_hours"], facts.get("food_category"))
    if limit is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"no limit defined for category {facts.get('food_category')}",
            missing=("max_hours",),
        )

    elapsed = _hours_between(start, end)
    ok = elapsed <= limit
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"{elapsed:.1f} h from {start_name} to {end_name}, limit {limit:.1f} h",
        observed=f"{elapsed:.1f} h", limit=f"<= {limit:.1f} h",
    )


def check_hours_between_at_least(rule_id: str, severity: str, facts: dict[str, Any],
                                 config: dict[str, Any], now: datetime,
                                 tz: ZoneInfo) -> RuleResult:
    start_name, end_name = config["start_field"], config["end_field"]
    start, end = facts.get(start_name), facts.get(end_name)
    missing = tuple(n for n, v in ((start_name, start), (end_name, end)) if v is None)
    if missing:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"cannot measure elapsed time: {', '.join(missing)} missing",
            missing=missing,
        )
    minimum = float(config["min_hours"])
    elapsed = _hours_between(start, end)
    ok = elapsed >= minimum
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"{elapsed:.1f} h elapsed, threshold {minimum:.1f} h",
        observed=f"{elapsed:.1f} h", limit=f">= {minimum:.1f} h",
    )


def check_conditional_flag_required(rule_id: str, severity: str, facts: dict[str, Any],
                                    config: dict[str, Any], now: datetime,
                                    tz: ZoneInfo) -> RuleResult:
    """A flag is only required when a nested condition holds.

    Example: non-veg food must be refrigerated, but only if it was held for
    more than an hour. If the condition does not apply, the rule passes
    vacuously rather than being skipped — so the audit trail says why.
    """
    condition = config["when"]
    condition_result = _run_check(
        f"{rule_id}:when", severity, facts, condition, now, tz
    )

    if condition_result.passed is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"precondition could not be evaluated: {condition_result.reason}",
            missing=condition_result.missing,
        )

    if not condition_result.passed:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=True,
            reason=f"not applicable: {condition_result.reason}",
        )

    flag_name = config["flag_field"]
    value = facts.get(flag_name)
    if value is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"{flag_name} required here but not recorded",
            missing=(flag_name,),
        )
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=bool(value),
        reason=f"condition applies and {flag_name} is {bool(value)}",
        observed=str(bool(value)), limit="must be true when the condition holds",
    )


def check_category_in_list(rule_id: str, severity: str, facts: dict[str, Any],
                           config: dict[str, Any], now: datetime,
                           tz: ZoneInfo) -> RuleResult:
    category = facts.get(config["donation_field"])
    accepted = facts.get(config["recipient_field"])
    if category is None or accepted is None:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason="category or accepted list missing",
            missing=tuple(
                n for n, v in (
                    (config["donation_field"], category),
                    (config["recipient_field"], accepted),
                ) if v is None
            ),
        )
    accepted_values = [str(a) for a in accepted]
    ok = str(category) in accepted_values
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=f"recipient {'accepts' if ok else 'does not accept'} {category}",
        observed=str(category), limit=", ".join(accepted_values) or "(none)",
    )


def check_window_overlaps_hours(rule_id: str, severity: str, facts: dict[str, Any],
                                config: dict[str, Any], now: datetime,
                                tz: ZoneInfo) -> RuleResult:
    start = facts.get(config["window_start_field"])
    end = facts.get(config["window_end_field"])
    opens = facts.get(config["opens_field"])
    closes = facts.get(config["closes_field"])

    missing = tuple(
        name for name, value in (
            (config["window_start_field"], start),
            (config["window_end_field"], end),
            (config["opens_field"], opens),
            (config["closes_field"], closes),
        ) if value is None
    )
    if missing:
        return RuleResult(
            rule_id=rule_id, severity=severity, passed=None,
            reason=f"cannot compare hours: {', '.join(missing)} missing",
            missing=missing,
        )

    ok = _window_overlaps(start, end, opens, closes, tz)
    return RuleResult(
        rule_id=rule_id, severity=severity, passed=ok,
        reason=(
            f"pickup window {'overlaps' if ok else 'falls outside'} "
            f"opening hours {opens}-{closes} local"
        ),
        observed=(
            f"{_as_local_time(start, tz)}-{_as_local_time(end, tz)} local"
        ),
        limit=f"{opens}-{closes}",
    )


CHECK_TYPES = {
    "field_present": check_field_present,
    "flag_is_true": check_flag_is_true,
    "timestamp_after_now": check_timestamp_after_now,
    "timestamp_order": check_timestamp_order,
    "numeric_min": check_numeric_min,
    "text_min_length": check_text_min_length,
    "hours_between_at_most": check_hours_between_at_most,
    "hours_between_at_least": check_hours_between_at_least,
    "conditional_flag_required": check_conditional_flag_required,
    "category_in_list": check_category_in_list,
    "window_overlaps_hours": check_window_overlaps_hours,
}


def _run_check(rule_id: str, severity: str, facts: dict[str, Any],
               config: dict[str, Any], now: datetime, tz: ZoneInfo) -> RuleResult:
    check_type = config.get("type")
    handler = CHECK_TYPES.get(check_type or "")
    if handler is None:
        raise UnknownCheckType(
            f"rule {rule_id!r} uses check type {check_type!r}, which the engine "
            f"does not implement. Known types: {', '.join(sorted(CHECK_TYPES))}"
        )
    return handler(rule_id, severity, facts, config, now, tz)
