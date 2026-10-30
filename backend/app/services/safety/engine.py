"""The safety rules engine.

Three properties make this safety-critical component defensible:

1. **It refuses to load a ruleset without a `policy_status`.** An illustrative
   demo policy cannot be silently mistaken for one verified against an
   authority.

2. **Missing evidence never passes.** A rule whose required fields are absent
   is UNEVALUATED, and any unevaluated rule forces NEEDS_REVIEW. There is no
   code path from "we don't know" to ELIGIBLE.

3. **The verdict is immutable.** `SafetyDecision` is a frozen dataclass built
   from frozen `RuleResult`s. An agent holding one cannot rewrite the outcome,
   and the engine exposes no setter. The database trigger is the second line
   of defence; this is the first.

Outcome resolution order, applied exactly as written:

    1. any HARD rule FAILED          -> REJECTED
    2. any rule UNEVALUATED          -> NEEDS_REVIEW
    3. any SOFT rule FAILED          -> NEEDS_REVIEW
    4. otherwise                     -> ELIGIBLE

Hard failure wins over everything, including over missing evidence elsewhere:
if the food is demonstrably too old, an unrelated absent field does not turn a
rejection into a review.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models.enums import SafetyStatus
from app.services.safety.checks import RuleResult, _run_check

log = get_logger(__name__)

RULESET_DIR = Path(__file__).parent / "rulesets"


class RulesetError(Exception):
    """A ruleset file is malformed, missing, or lacks a policy status."""


@dataclass(frozen=True)
class SafetyDecision:
    """An immutable verdict. Nothing downstream can change `outcome`."""

    outcome: SafetyStatus
    rule_version: str
    policy_status: str
    results: tuple[RuleResult, ...]
    unevaluated: tuple[str, ...]

    @property
    def is_eligible(self) -> bool:
        return self.outcome == SafetyStatus.ELIGIBLE

    @property
    def hard_failures(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.severity == "hard" and r.passed is False)

    @property
    def soft_failures(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.severity == "soft" and r.passed is False)

    def summary(self) -> str:
        """One line for the workflow event timeline."""
        if self.outcome == SafetyStatus.REJECTED:
            names = ", ".join(r.rule_id for r in self.hard_failures)
            return f"Rejected: {names}"
        if self.outcome == SafetyStatus.NEEDS_REVIEW:
            if self.unevaluated:
                return f"Needs review: missing evidence for {', '.join(self.unevaluated)}"
            names = ", ".join(r.rule_id for r in self.soft_failures)
            return f"Needs review: {names}"
        return f"Eligible under {self.rule_version}"

    def to_per_rule(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.results]


@dataclass(frozen=True)
class Ruleset:
    version: str
    policy_status: str
    source_note: str | None
    rules: tuple[dict[str, Any], ...]
    recipient_rules: tuple[dict[str, Any], ...]
    raw: dict[str, Any]


def load_ruleset(version: str | None = None) -> Ruleset:
    """Load and validate a ruleset file.

    Raises RulesetError if `policy_status` is absent. That is deliberate and
    load-bearing: it is what stops a demo policy being mistaken for a verified
    one. Do not "fix" this by defaulting the field.
    """
    version = version or get_settings().safety_ruleset_version
    path = RULESET_DIR / f"{version}.yaml"

    if not path.exists():
        raise RulesetError(
            f"ruleset {version!r} not found at {path}. "
            f"Available: {', '.join(p.stem for p in RULESET_DIR.glob('*.yaml')) or 'none'}"
        )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RulesetError(f"ruleset {version!r} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise RulesetError(f"ruleset {version!r} must be a YAML mapping")

    policy_status = data.get("policy_status")
    if not policy_status or not str(policy_status).strip():
        raise RulesetError(
            f"ruleset {version!r} has no policy_status. Every ruleset must state "
            f"whether it is an illustrative demo or has been verified against an "
            f"authority. Refusing to load it."
        )

    rules = data.get("rules") or []
    if not rules:
        raise RulesetError(f"ruleset {version!r} defines no rules")

    for rule in rules:
        for required in ("id", "severity", "check"):
            if required not in rule:
                raise RulesetError(
                    f"ruleset {version!r}: rule {rule.get('id', '<unnamed>')!r} "
                    f"is missing {required!r}"
                )
        if rule["severity"] not in ("hard", "soft"):
            raise RulesetError(
                f"ruleset {version!r}: rule {rule['id']!r} has severity "
                f"{rule['severity']!r}; must be 'hard' or 'soft'"
            )

    return Ruleset(
        version=data.get("version", version),
        policy_status=str(policy_status),
        source_note=data.get("source_note"),
        rules=tuple(rules),
        recipient_rules=tuple(data.get("recipient_rules") or []),
        raw=data,
    )


@lru_cache(maxsize=8)
def get_ruleset(version: str | None = None) -> Ruleset:
    """Cached loader. Rulesets are immutable files, so caching is safe."""
    return load_ruleset(version)


def _applies_to(rule: dict[str, Any], category: str | None) -> bool:
    """`applies_to: [ALL]` or an absent key means every category."""
    applies = rule.get("applies_to")
    if not applies or "ALL" in applies:
        return True
    return category is not None and str(category) in applies


def evaluate(
    facts: dict[str, Any],
    *,
    ruleset: Ruleset | None = None,
    now: datetime | None = None,
    include_recipient_rules: bool = False,
) -> SafetyDecision:
    """Evaluate every applicable rule and resolve a single outcome.

    `facts` is a flat dict of donation (and optionally recipient) attributes.
    Building it is the caller's job — see `facts_from_donation` below — which
    keeps this function pure and testable with hand-written dicts.
    """
    ruleset = ruleset or get_ruleset()
    now = now or datetime.now(UTC)
    tz = ZoneInfo(get_settings().default_timezone)
    category = facts.get("food_category")
    category = str(category) if category is not None else None

    rules = list(ruleset.rules)
    if include_recipient_rules:
        rules.extend(ruleset.recipient_rules)

    results: list[RuleResult] = []
    for rule in rules:
        if not _applies_to(rule, category):
            continue

        result = _run_check(
            rule["id"], rule["severity"], facts, rule["check"], now, tz
        )

        # A rule may declare `on_missing_evidence: reject`, which upgrades an
        # unevaluated result into a hard failure. Used for structural facts
        # (a quantity, a pickup window) whose absence is a malformed listing
        # rather than an open question for a reviewer.
        if result.passed is None and rule.get("on_missing_evidence") == "reject":
            result = RuleResult(
                rule_id=result.rule_id,
                severity="hard",
                passed=False,
                reason=f"{result.reason} (treated as a failure by policy)",
                observed=result.observed,
                limit=result.limit,
                missing=result.missing,
            )

        results.append(result)

    unevaluated = tuple(r.rule_id for r in results if r.passed is None)

    # --- outcome resolution, in this exact order ---
    if any(r.severity == "hard" and r.passed is False for r in results):
        outcome = SafetyStatus.REJECTED
    elif unevaluated:
        outcome = SafetyStatus.NEEDS_REVIEW
    elif any(r.severity == "soft" and r.passed is False for r in results):
        outcome = SafetyStatus.NEEDS_REVIEW
    else:
        outcome = SafetyStatus.ELIGIBLE

    return SafetyDecision(
        outcome=outcome,
        rule_version=ruleset.version,
        policy_status=ruleset.policy_status,
        results=tuple(results),
        unevaluated=unevaluated,
    )


def facts_from_donation(donation: Any, donor_org: Any = None) -> dict[str, Any]:
    """Flatten a Donation ORM object into the fact dict the engine consumes.

    Kept separate from `evaluate` so the engine never touches the ORM and can
    be tested with plain dictionaries.
    """
    return {
        "food_category": donation.food_category,
        "unit": donation.unit,
        "quantity_total": donation.quantity_total,
        "prepared_at": donation.prepared_at,
        "pickup_window_start": donation.pickup_window_start,
        "pickup_window_end": donation.pickup_window_end,
        "storage_notes": donation.storage_notes,
        "kept_refrigerated": donation.kept_refrigerated,
        "allergen_notes": donation.allergen_notes,
        "donor_org_is_verified": (
            donor_org.is_verified if donor_org is not None else None
        ),
    }


def facts_for_recipient(donation: Any, recipient_org: Any) -> dict[str, Any]:
    """Facts for the recipient-side eligibility rules, evaluated at match time
    because they depend on WHICH recipient is claiming."""
    return {
        "food_category": donation.food_category,
        "pickup_window_start": donation.pickup_window_start,
        "pickup_window_end": donation.pickup_window_end,
        "accepted_categories": recipient_org.accepted_categories or [],
        "has_refrigeration": recipient_org.has_refrigeration,
        "opens_at": recipient_org.opens_at,
        "closes_at": recipient_org.closes_at,
    }
