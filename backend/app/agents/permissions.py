"""Tool permissions, enforced at call time.

This file is the structural answer to "how do you know an agent cannot bypass
your safety check". Each node is granted a frozen set of tool names, and the
`@tool` decorator refuses anything outside it.

The entry that matters most is the one that does not exist: **no node anywhere
is granted `record_handover`.** Handover is written only by an authenticated
API request carrying a confirmation code, so an LLM cannot mark a donation
delivered by generating a response — not because it is told not to, but
because it has no tool that could.

`test_permissions.py` asserts that stays true, so the guarantee survives
someone adding a tool in six months without reading this comment.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from app.core.logging import get_logger

log = get_logger(__name__)

TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "extract_fields": frozenset({
        "extract_donation_fields",
        "normalize_units",
    }),
    "validate_listing": frozenset({
        "validate_listing",
        "find_duplicate",
        "upsert_donation",
    }),
    "safety_evaluate": frozenset({
        "load_rule_version",
        "evaluate_rules",
        "record_safety_check",
    }),
    "find_and_rank_recipients": frozenset({
        "discover_candidates",
        "score_candidates",
        "persist_matches",
    }),
    "reserve_quantity": frozenset({
        "reserve_quantity",
        "release_reservation",
    }),
    "await_confirmation": frozenset({
        "schedule_timer",
        "enqueue_notification",
    }),
    "assign_pickup": frozenset({
        "find_volunteer",
        "estimate_route",
        "create_pickup",
    }),
    "update_impact": frozenset({
        "compute_metrics",
        "summarize_metrics",
    }),
    "human_safety_review": frozenset({
        "enqueue_notification",
    }),
    "close_workflow": frozenset(),
}

# Tools that exist in the codebase but are granted to NO node, ever.
# Listed explicitly so the prohibition is visible rather than implied.
NEVER_GRANTED: frozenset[str] = frozenset({
    "record_handover",      # needs a verified code and an authenticated human
    "resolve_safety_review",  # needs a human reviewer, attributed in the DB
    "set_safety_status",    # only donation_service.evaluate_safety may do this
    "delete_donation",
    "promote_user",
})


class ToolPermissionError(Exception):
    """A node attempted a tool it was not granted."""


F = TypeVar("F", bound=Callable[..., Any])


def tool(name: str) -> Callable[[F], F]:
    """Mark a function as a tool, enforcing the node's permissions.

    The wrapped function must receive `node` as a keyword argument, which the
    graph supplies. A tool called with no node context is refused rather than
    allowed — failing closed is the only safe default here.
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, node: str | None = None, **kwargs: Any) -> Any:
            if name in NEVER_GRANTED:
                log.error(
                    "a never-granted tool was invoked through the agent layer",
                    extra={"context": {"tool": name, "node": node}},
                )
                raise ToolPermissionError(
                    f"tool {name!r} is never granted to any workflow node"
                )

            if node is None:
                raise ToolPermissionError(
                    f"tool {name!r} was called without node context; refusing"
                )

            allowed = TOOL_PERMISSIONS.get(node)
            if allowed is None:
                raise ToolPermissionError(f"unknown node {node!r}")

            if name not in allowed:
                log.error(
                    "tool permission denied",
                    extra={"context": {"tool": name, "node": node}},
                )
                raise ToolPermissionError(
                    f"node {node!r} is not permitted to call tool {name!r}. "
                    f"Permitted: {', '.join(sorted(allowed)) or '(none)'}"
                )

            return await func(*args, **kwargs)

        wrapper._tool_name = name       # type: ignore[attr-defined]
        return wrapper                  # type: ignore[return-value]

    return decorator


def tools_for(node: str) -> frozenset[str]:
    return TOOL_PERMISSIONS.get(node, frozenset())


def all_granted_tools() -> frozenset[str]:
    granted: set[str] = set()
    for names in TOOL_PERMISSIONS.values():
        granted |= names
    return frozenset(granted)
