"""Graph construction, isolated in one file on purpose.

LangGraph's API has moved considerably between versions. Keeping every call to
it in this single module means an upgrade touches one file, while the node
functions in `nodes.py` and the branch functions in `routers.py` keep working
and keep their tests passing. That is a deliberate hedge, not an accident of
layout.

If LangGraph is unavailable or its API has changed, `build_graph` raises a
clear error and `run_sequential` provides the same traversal in plain Python —
same nodes, same routers, same events, without the checkpointing. The fallback
exists so a LangGraph problem cannot block a demo, and it is NOT a substitute:
without durable checkpoints the resume guarantee is gone, which is most of why
LangGraph is here at all.
"""

import uuid
from typing import Any

from app.core.logging import get_logger
from app.workflows import routers
from app.workflows.nodes import NodeContext, ensure_run, make_nodes
from app.workflows.state import FoodLoopState, initial_state

log = get_logger(__name__)

# Node names that end a traversal.
TERMINAL_NODES = frozenset({
    "close_workflow", "close_rejected", "no_recipient_hold",
    "pending_info", "escalate_human",
})

# Nodes that pause for a human or a timer. The graph interrupts here; a
# resume re-enters the same node and re-checks whether the wait is over.
INTERRUPT_NODES = frozenset({
    "request_clarification", "human_safety_review",
    "await_confirmation", "await_handover",
})


class LangGraphUnavailable(RuntimeError):
    """langgraph is not installed, or its API differs from what we expect."""


def build_graph(ctx: NodeContext, *, checkpointer: Any = None) -> Any:
    """Build the compiled LangGraph.

    Requires the `agents` extra:  uv sync --extra agents
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailable(
            "langgraph is not installed. Install it with "
            "`uv sync --extra agents`, or use run_sequential() for a "
            "checkpoint-free traversal."
        ) from exc

    nodes = make_nodes(ctx)
    builder = StateGraph(FoodLoopState)

    for name, fn in nodes.items():
        builder.add_node(name, fn)

    builder.add_edge(START, "extract_fields")

    # --- conditional edges, each delegating to a PURE router function ---
    builder.add_conditional_edges(
        "extract_fields",
        routers.route_after_extraction,
        {
            "validate_listing": "validate_listing",
            "request_clarification": "request_clarification",
            "pending_info": "pending_info",
        },
    )
    # The clarification loop. Bounded by MAX_CLARIFICATION_ROUNDS inside the
    # router, so this cycle terminates by design.
    builder.add_edge("request_clarification", "extract_fields")

    builder.add_conditional_edges(
        "validate_listing",
        routers.route_after_validation,
        {"safety_evaluate": "safety_evaluate", "escalate_human": "escalate_human"},
    )

    builder.add_conditional_edges(
        "safety_evaluate",
        routers.route_after_safety,
        {
            "find_and_rank_recipients": "find_and_rank_recipients",
            "human_safety_review": "human_safety_review",
            "close_rejected": "close_rejected",
        },
    )
    # Review resumes by re-entering safety_evaluate, which re-reads the status
    # a human set. There is no edge that skips the gate.
    builder.add_edge("human_safety_review", "safety_evaluate")

    builder.add_conditional_edges(
        "find_and_rank_recipients",
        routers.route_after_matching,
        {
            "reserve_quantity": "reserve_quantity",
            "no_recipient_hold": "no_recipient_hold",
        },
    )

    # The reservation-conflict loop. Bounded by MAX_CANDIDATE_ATTEMPTS.
    builder.add_conditional_edges(
        "reserve_quantity",
        routers.route_after_reservation,
        {
            "await_confirmation": "await_confirmation",
            "reserve_quantity": "reserve_quantity",
            "no_recipient_hold": "no_recipient_hold",
        },
    )

    builder.add_conditional_edges(
        "await_confirmation",
        routers.route_after_confirmation,
        {
            "assign_pickup": "assign_pickup",
            "await_confirmation": "await_confirmation",
            "reserve_quantity": "reserve_quantity",
            "no_recipient_hold": "no_recipient_hold",
        },
    )

    builder.add_conditional_edges(
        "assign_pickup",
        routers.route_after_pickup,
        {"await_handover": "await_handover", "escalate_human": "escalate_human"},
    )

    builder.add_conditional_edges(
        "await_handover",
        routers.route_after_handover,
        {"update_impact": "update_impact", "await_handover": "await_handover"},
    )

    builder.add_edge("update_impact", "close_workflow")

    for terminal in TERMINAL_NODES:
        builder.add_edge(terminal, END)

    # interrupt_before is what makes the human-in-the-loop steps real: the
    # graph stops, the checkpoint persists, and a later resume continues from
    # exactly here rather than re-running the whole donation.
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=sorted(INTERRUPT_NODES),
    )


async def make_checkpointer(connection_string: str) -> Any:
    """Postgres checkpointer. Durability is the reason LangGraph is here.

    An in-memory saver would make the resume test pass within one process and
    fail the moment anything restarts, which is the opposite of the guarantee
    being claimed.
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise LangGraphUnavailable(
            "langgraph-checkpoint-postgres is not installed. "
            "Install it with `uv sync --extra agents`."
        ) from exc

    # psycopg wants a plain DSN, not SQLAlchemy's driver-prefixed URL.
    dsn = connection_string.replace("postgresql+psycopg://", "postgresql://")
    saver = AsyncPostgresSaver.from_conn_string(dsn)
    return saver


# ---------------------------------------------------------------------------
# The fallback traversal: same nodes, same routers, no checkpointing.
# ---------------------------------------------------------------------------

ROUTES: dict[str, Any] = {
    "extract_fields": routers.route_after_extraction,
    "validate_listing": routers.route_after_validation,
    "safety_evaluate": routers.route_after_safety,
    "find_and_rank_recipients": routers.route_after_matching,
    "reserve_quantity": routers.route_after_reservation,
    "await_confirmation": routers.route_after_confirmation,
    "assign_pickup": routers.route_after_pickup,
    "await_handover": routers.route_after_handover,
}

STATIC_EDGES: dict[str, str] = {
    "request_clarification": "extract_fields",
    "human_safety_review": "safety_evaluate",
    "update_impact": "close_workflow",
}


async def run_sequential(
    ctx: NodeContext,
    *,
    donation_id: uuid.UUID,
    raw_description: str | None = None,
    max_steps: int = 40,
    stop_on_interrupt: bool = True,
) -> FoodLoopState:
    """Walk the same graph in plain Python.

    Uses the identical node and router functions, so behaviour matches — but
    there is no checkpoint, so a crash loses the run. Use it to demonstrate
    the flow when LangGraph is unavailable, never as the production path.

    `max_steps` is a safety net for a malformed graph, not a loop bound: the
    real bounds live in the routers' attempt counters.
    """
    async with ctx.sessionmaker() as session:
        run = await ensure_run(session, donation_id=donation_id)

    state = initial_state(
        donation_id=str(donation_id),
        run_id=str(run.id),
        raw_description=raw_description,
    )

    nodes = make_nodes(ctx)
    current = "extract_fields"

    for step in range(max_steps):
        node_fn = nodes.get(current)
        if node_fn is None:
            state["last_error"] = f"unknown node {current!r}"
            break

        update = await node_fn(state)

        # Merge, applying the attempts reducer so a counter never regresses.
        for key, value in update.items():
            if key == "attempts":
                from app.workflows.state import merge_attempts

                state["attempts"] = merge_attempts(
                    state.get("attempts") or {}, value
                )
            else:
                state[key] = value   # type: ignore[literal-required]

        if current in TERMINAL_NODES:
            log.info(
                "workflow finished",
                extra={"context": {
                    "donation_id": str(donation_id),
                    "terminal": current,
                    "steps": step + 1,
                }},
            )
            break

        if stop_on_interrupt and state.get("needs_human") and current in INTERRUPT_NODES:
            log.info(
                "workflow interrupted",
                extra={"context": {
                    "donation_id": str(donation_id),
                    "node": current,
                    "reason": state.get("needs_human"),
                }},
            )
            break

        if current in STATIC_EDGES:
            current = STATIC_EDGES[current]
            continue

        router = ROUTES.get(current)
        if router is None:
            state["last_error"] = f"no route out of {current!r}"
            break
        current = router(state)
    else:
        state["last_error"] = f"exceeded {max_steps} steps without terminating"
        log.error(
            "workflow exceeded its step ceiling — check the routers for a "
            "cycle without an attempt counter",
            extra={"context": {"donation_id": str(donation_id)}},
        )

    return state
