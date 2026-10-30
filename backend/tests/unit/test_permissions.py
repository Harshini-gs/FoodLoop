"""Tool permission tests.

The headline assertion is `test_no_node_can_record_a_handover`. It is what
makes "an LLM cannot mark a donation delivered" a tested property rather than
a sentence in a README, and it will fail if someone later grants that tool to
a node without reading the comments.
"""

import pytest

from app.agents.permissions import (
    NEVER_GRANTED,
    TOOL_PERMISSIONS,
    ToolPermissionError,
    all_granted_tools,
    tool,
    tools_for,
)


def test_no_node_can_record_a_handover() -> None:
    """THE test. No node, anywhere, may write a handover event.

    Collection and delivery are recorded only by an authenticated API request
    carrying a confirmation code. If this test fails, the project's central
    claim is no longer true.
    """
    for node, granted in TOOL_PERMISSIONS.items():
        assert "record_handover" not in granted, (
            f"node {node!r} was granted record_handover — an agent could now "
            f"mark a donation delivered"
        )


def test_never_granted_tools_are_granted_to_nobody() -> None:
    granted = all_granted_tools()
    overlap = granted & NEVER_GRANTED
    assert not overlap, f"these tools must never be granted: {sorted(overlap)}"


def test_no_node_can_set_safety_status_directly() -> None:
    for node, granted in TOOL_PERMISSIONS.items():
        assert "set_safety_status" not in granted, (
            f"node {node!r} could set safety_status directly, bypassing the "
            f"rules engine"
        )


def test_no_node_can_resolve_a_safety_review() -> None:
    """Review is a human decision, attributed in the database."""
    for node, granted in TOOL_PERMISSIONS.items():
        assert "resolve_safety_review" not in granted, (
            f"node {node!r} could approve its own safety review"
        )


def test_safety_node_cannot_reserve_quantity() -> None:
    """Separation of duties: the node that evaluates safety cannot act on it."""
    assert "reserve_quantity" not in tools_for("safety_evaluate")


def test_reservation_node_cannot_evaluate_safety() -> None:
    assert "evaluate_rules" not in tools_for("reserve_quantity")


def test_matching_node_cannot_move_quantity() -> None:
    granted = tools_for("find_and_rank_recipients")
    assert "reserve_quantity" not in granted
    assert "release_reservation" not in granted


# ---------------------------------------------------------------------------
# the decorator
# ---------------------------------------------------------------------------

@tool("evaluate_rules")
async def _fake_evaluate() -> str:
    return "ok"


@tool("record_handover")
async def _fake_handover() -> str:
    return "should never run"


async def test_permitted_call_succeeds() -> None:
    assert await _fake_evaluate(node="safety_evaluate") == "ok"


async def test_unpermitted_call_is_refused() -> None:
    with pytest.raises(ToolPermissionError, match="not permitted"):
        await _fake_evaluate(node="reserve_quantity")


async def test_never_granted_tool_is_refused_from_every_node() -> None:
    """Even a node that somehow had it in its list could not call it."""
    for node in TOOL_PERMISSIONS:
        with pytest.raises(ToolPermissionError, match="never granted"):
            await _fake_handover(node=node)


async def test_call_without_node_context_is_refused() -> None:
    """Fails closed. A tool invoked with no node context is refused rather
    than allowed, because the alternative is a bypass."""
    with pytest.raises(ToolPermissionError, match="without node context"):
        await _fake_evaluate()


async def test_unknown_node_is_refused() -> None:
    with pytest.raises(ToolPermissionError, match="unknown node"):
        await _fake_evaluate(node="not_a_real_node")


def test_every_node_in_the_graph_has_a_permission_entry() -> None:
    """A node with no entry would be refused at call time, which is safe but
    confusing. Catch it here instead."""
    from app.workflows.graph import TERMINAL_NODES

    graph_nodes = {
        "extract_fields", "validate_listing", "safety_evaluate",
        "find_and_rank_recipients", "reserve_quantity", "await_confirmation",
        "assign_pickup", "update_impact", "human_safety_review",
    }
    missing = graph_nodes - set(TOOL_PERMISSIONS)
    assert not missing, f"these nodes have no permission entry: {sorted(missing)}"
