"""Workflow nodes.

Each node is a thin wrapper over a business service. The orchestration layer
decides WHAT happens next; the services decide HOW. That separation is why the
safety engine, the reservation repository and the matching service all have
their own tests that never mention LangGraph.

Every node writes a `workflow_events` row on entry and exit. That table is
what the frontend's workflow view renders — the UI shows rows that exist
rather than an animation, which is the difference between a workflow
visualization and a decoration.
"""

import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ReservationConflict, SafetyGateViolation
from app.core.logging import get_logger
from app.models import Donation, WorkflowEvent, WorkflowRun
from app.models.enums import EventPhase, WorkflowRunStatus
from app.repositories import reservations as reservation_repo
from app.services import donation as donation_service
from app.services import handover as handover_service
from app.services import impact as impact_service
from app.services import matching as matching_service
from app.workflows.state import (
    ATTEMPT_CANDIDATE,
    ATTEMPT_CLARIFICATION,
    FoodLoopState,
    bumped,
)

log = get_logger(__name__)


async def record_event(
    session: AsyncSession,
    *,
    state: FoodLoopState,
    node: str,
    phase: EventPhase,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append a workflow event. The source of truth for the workflow UI."""
    run_id = state.get("run_id")
    donation_id = state.get("donation_id")
    if not run_id or not donation_id:
        return

    event = WorkflowEvent(
        run_id=uuid.UUID(run_id),
        donation_id=uuid.UUID(donation_id),
        node=node,
        phase=phase,
        from_state=from_state,
        to_state=to_state,
        summary=summary,
        payload=payload,
        attempt=1,
        duration_ms=duration_ms,
    )
    session.add(event)
    await session.commit()


async def ensure_run(
    session: AsyncSession, *, donation_id: uuid.UUID
) -> WorkflowRun:
    """Get or create the run row for a donation.

    One run per donation, keyed by thread_id, so a resumed workflow attaches
    to the same history rather than starting a parallel one.
    """
    thread_id = f"donation:{donation_id}"
    result = await session.execute(
        select(WorkflowRun).where(WorkflowRun.thread_id == thread_id)
    )
    run = result.scalars().first()
    if run is not None:
        return run

    run = WorkflowRun(
        thread_id=thread_id,
        donation_id=donation_id,
        status=WorkflowRunStatus.RUNNING,
        current_node="ingest_donation",
        attempts={},
    )
    session.add(run)
    await session.commit()
    return run


class NodeContext:
    """Carries the session into nodes.

    LangGraph nodes receive only state, which must stay JSON-serializable, so
    a database session cannot live there. The graph is built with a context
    holding the sessionmaker and each node opens its own session.
    """

    def __init__(self, sessionmaker: Any) -> None:
        self.sessionmaker = sessionmaker


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def make_nodes(ctx: NodeContext) -> dict[str, Any]:
    """Build the node functions, closing over the context."""

    async def _event(state: FoodLoopState, node: str, phase: EventPhase, **kw: Any) -> None:
        async with ctx.sessionmaker() as session:
            await record_event(session, state=state, node=node, phase=phase, **kw)

    async def extract_fields(state: FoodLoopState) -> dict[str, Any]:
        """The one LLM-driven step. Produces a draft, never a donation."""
        started = time.monotonic()
        await _event(state, "extract_fields", EventPhase.ENTER)

        raw = state.get("raw_description")
        answer = state.get("clarification_answer")
        if answer:
            raw = f"{raw or ''}\n{answer}"

        if not raw:
            # Structured submission: nothing to extract.
            await _event(
                state, "extract_fields", EventPhase.EXIT,
                summary="structured submission, no extraction needed",
            )
            return {"missing_fields": [], "node_history": [*state.get("node_history", []), "extract_fields"]}

        from app.schemas.donation import DonationFromText

        draft = await donation_service.extract_from_text(
            DonationFromText(raw_description=raw)
        )

        await _event(
            state, "extract_fields", EventPhase.EXIT,
            summary=(
                f"extracted {len(MANDATORY) - len(draft.missing_fields)}/{len(MANDATORY)} "
                f"mandatory fields"
            ),
            payload={
                "missing_fields": draft.missing_fields,
                "ambiguities": draft.ambiguities,
                "normalizations": draft.unit_normalizations,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        return {
            "draft": draft.model_dump(mode="json"),
            "missing_fields": draft.missing_fields,
            "clarification_question": draft.clarification_question,
            "node_history": [*state.get("node_history", []), "extract_fields"],
        }

    async def request_clarification(state: FoodLoopState) -> dict[str, Any]:
        """Interrupt: ask the donor, then resume. Bounded at 2 rounds."""
        await _event(
            state, "request_clarification", EventPhase.INTERRUPT,
            summary=state.get("clarification_question") or "missing information",
            payload={"missing_fields": state.get("missing_fields")},
        )
        return {
            "attempts": bumped(state, ATTEMPT_CLARIFICATION),
            "needs_human": "clarification",
            "node_history": [*state.get("node_history", []), "request_clarification"],
        }

    async def validate_listing(state: FoodLoopState) -> dict[str, Any]:
        await _event(state, "validate_listing", EventPhase.ENTER)
        async with ctx.sessionmaker() as session:
            donation = await donation_service.get_donation(
                session, uuid.UUID(state["donation_id"])
            )
            ok = donation.quantity_total > 0 and (
                donation.pickup_window_end > donation.pickup_window_start
            )

        await _event(
            state, "validate_listing", EventPhase.EXIT,
            summary="listing is well-formed" if ok else "listing failed validation",
        )
        return {
            "last_error": None if ok else "listing failed structural validation",
            "node_history": [*state.get("node_history", []), "validate_listing"],
        }

    async def safety_evaluate(state: FoodLoopState) -> dict[str, Any]:
        """The hard gate. Deterministic rules, no LLM anywhere in the path."""
        started = time.monotonic()
        await _event(state, "safety_evaluate", EventPhase.ENTER)

        async with ctx.sessionmaker() as session:
            decision, check = await donation_service.evaluate_safety(
                session, donation_id=uuid.UUID(state["donation_id"])
            )

        await _event(
            state, "safety_evaluate", EventPhase.EXIT,
            summary=decision.summary(),
            to_state=decision.outcome.value,
            payload={
                "outcome": decision.outcome.value,
                "rule_version": decision.rule_version,
                "policy_status": decision.policy_status,
                "failed_rules": [r.rule_id for r in decision.hard_failures],
                "unevaluated": list(decision.unevaluated),
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        return {
            "safety_outcome": decision.outcome.value,
            "safety_summary": decision.summary(),
            "safety_check_id": str(check.id),
            "node_history": [*state.get("node_history", []), "safety_evaluate"],
        }

    async def human_safety_review(state: FoodLoopState) -> dict[str, Any]:
        """Interrupt, with no automatic way out. Deliberately."""
        await _event(
            state, "human_safety_review", EventPhase.INTERRUPT,
            summary=state.get("safety_summary") or "awaiting human safety review",
        )
        async with ctx.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE workflow_runs SET status = 'WAITING_HUMAN', "
                    "interrupt_reason = 'safety_review' WHERE thread_id = :tid"
                ),
                {"tid": state["thread_id"]},
            )
            await session.commit()

        return {
            "needs_human": "safety_review",
            "node_history": [*state.get("node_history", []), "human_safety_review"],
        }

    async def find_and_rank_recipients(state: FoodLoopState) -> dict[str, Any]:
        started = time.monotonic()
        await _event(state, "find_and_rank_recipients", EventPhase.ENTER)

        async with ctx.sessionmaker() as session:
            donation = await donation_service.get_donation(
                session, uuid.UUID(state["donation_id"])
            )
            needs_cold = donation.food_category.value in ("DAIRY", "COOKED_NONVEG")
            kept, excluded, radius, widenings = (
                await matching_service.find_candidates_widening(
                    session,
                    donation_id=donation.id,
                    food_category=donation.food_category.value,
                    quantity=donation.quantity_total - donation.quantity_reserved,
                    window_start=donation.pickup_window_start,
                    window_end=donation.pickup_window_end,
                    needs_refrigeration=needs_cold,
                )
            )
            if kept:
                await matching_service.persist_matches(
                    session, donation_id=donation.id, candidates=kept
                )

        await _event(
            state, "find_and_rank_recipients", EventPhase.EXIT,
            summary=(
                f"{len(kept)} eligible recipient(s), {len(excluded)} excluded, "
                f"radius {radius} m"
            ),
            payload={
                "top_candidate": kept[0].recipient_name if kept else None,
                "top_score": kept[0].score if kept else None,
                "score_components": kept[0].components if kept else None,
                "excluded": [
                    {"name": e.recipient_name, "reason": e.reason} for e in excluded
                ],
                "widenings_applied": widenings,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        return {
            "candidates": [
                {
                    "recipient_org_id": str(c.recipient_org_id),
                    "recipient_name": c.recipient_name,
                    "score": c.score,
                    "components": c.components,
                    "distance_meters": c.distance_meters,
                    "allocatable_quantity": str(c.allocatable_quantity),
                }
                for c in kept
            ],
            "excluded": [
                {"recipient_name": e.recipient_name, "reason": e.reason}
                for e in excluded
            ],
            "candidate_index": 0,
            "search_radius_m": radius,
            "node_history": [*state.get("node_history", []), "find_and_rank_recipients"],
        }

    async def reserve_quantity(state: FoodLoopState) -> dict[str, Any]:
        """Attempt the transaction. A conflict is a normal outcome."""
        started = time.monotonic()
        index = state.get("candidate_index", 0)
        candidates = state.get("candidates") or []

        if index >= len(candidates):
            return {
                "reservation_error": "no candidates left to try",
                "node_history": [*state.get("node_history", []), "reserve_quantity"],
            }

        candidate = candidates[index]
        await _event(
            state, "reserve_quantity", EventPhase.ENTER,
            summary=f"attempting {candidate['recipient_name']} (rank {index + 1})",
        )

        # Idempotency key derived from (run, node, attempt): a replayed node
        # returns the original reservation instead of creating a second hold.
        key = f"{state['run_id']}:reserve:{index}"

        try:
            async with ctx.sessionmaker() as session:
                reservation = await reservation_repo.reserve(
                    session,
                    donation_id=uuid.UUID(state["donation_id"]),
                    recipient_org_id=uuid.UUID(candidate["recipient_org_id"]),
                    quantity=Decimal(candidate["allocatable_quantity"]),
                    idempotency_key=key,
                )
        except (ReservationConflict, SafetyGateViolation) as exc:
            await _event(
                state, "reserve_quantity", EventPhase.ERROR,
                summary=f"{candidate['recipient_name']}: {exc.message}",
                payload={"error_code": exc.code, "candidate_index": index},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {
                "reservation_id": None,
                "reservation_error": exc.message,
                "candidate_index": index + 1,
                "attempts": bumped(state, ATTEMPT_CANDIDATE),
                "node_history": [*state.get("node_history", []), "reserve_quantity"],
            }

        await _event(
            state, "reserve_quantity", EventPhase.EXIT,
            summary=f"reserved {reservation.quantity} for {candidate['recipient_name']}",
            payload={
                "reservation_id": str(reservation.id),
                "recipient": candidate["recipient_name"],
                "expires_at": reservation.expires_at.isoformat(),
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        return {
            "reservation_id": str(reservation.id),
            "reserved_quantity": str(reservation.quantity),
            "reservation_error": None,
            "attempts": bumped(state, ATTEMPT_CANDIDATE),
            "node_history": [*state.get("node_history", []), "reserve_quantity"],
        }

    async def await_confirmation(state: FoodLoopState) -> dict[str, Any]:
        """Interrupt plus a durable timer.

        The timer is a `scheduled_tasks` row, not a sleep. A process restart
        loses nothing: the sweeper fires the timeout from the database.
        """
        from app.core.settings import get_settings
        from app.providers import notify

        settings = get_settings()
        await _event(
            state, "await_confirmation", EventPhase.INTERRUPT,
            summary=f"awaiting recipient confirmation (expires in "
                    f"{settings.confirmation_timeout_minutes} min)",
        )

        async with ctx.sessionmaker() as session:
            notify.enqueue(
                session,
                template="offer_proposed",
                donation_id=uuid.UUID(state["donation_id"]),
                payload={"reservation_id": state.get("reservation_id")},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO scheduled_tasks (id, kind, due_at, payload, status, dedupe_key)
                    VALUES (gen_random_uuid(), 'CONFIRMATION_TIMEOUT',
                            now() + make_interval(mins => :mins),
                            CAST(:payload AS jsonb), 'PENDING', :dedupe)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    """
                ),
                {
                    "mins": settings.confirmation_timeout_minutes,
                    "payload": f'{{"reservation_id": "{state.get("reservation_id")}"}}',
                    "dedupe": f"confirm:{state.get('reservation_id')}",
                },
            )
            await session.commit()

        return {
            "needs_human": "recipient_confirmation",
            "node_history": [*state.get("node_history", []), "await_confirmation"],
        }

    async def assign_pickup(state: FoodLoopState) -> dict[str, Any]:
        started = time.monotonic()
        await _event(state, "assign_pickup", EventPhase.ENTER)

        try:
            async with ctx.sessionmaker() as session:
                pickup, _code = await handover_service.assign_pickup(
                    session, reservation_id=uuid.UUID(state["reservation_id"])
                )
        except Exception as exc:  # noqa: BLE001 — infeasibility is explained
            await _event(
                state, "assign_pickup", EventPhase.ERROR,
                summary=f"could not assign a pickup: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {
                "last_error": str(exc),
                "node_history": [*state.get("node_history", []), "assign_pickup"],
            }

        await _event(
            state, "assign_pickup", EventPhase.EXIT,
            summary=(
                f"pickup scheduled, {pickup.estimated_distance_km} km "
                f"({pickup.data_source.value.lower()})"
            ),
            payload={
                "pickup_id": str(pickup.id),
                "distance_km": str(pickup.estimated_distance_km),
                "data_source": pickup.data_source.value,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        return {
            "pickup_id": str(pickup.id),
            "node_history": [*state.get("node_history", []), "assign_pickup"],
        }

    async def await_handover(state: FoodLoopState) -> dict[str, Any]:
        """Wait for verified handover events.

        The graph cannot advance this itself. A delivery exists only when a
        human confirmed it with a code, which is the structural reason no LLM
        can close a donation.
        """
        async with ctx.sessionmaker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT h.kind, h.quantity FROM handover_events h
                      JOIN pickups p ON p.id = h.pickup_id
                     WHERE p.id = :pickup_id
                    """
                ),
                {"pickup_id": state.get("pickup_id")},
            )
            events = {row[0]: row[1] for row in result.all()}

        collected = events.get("COLLECT")
        delivered = events.get("DELIVER")

        if delivered is None:
            await _event(
                state, "await_handover", EventPhase.INTERRUPT,
                summary=(
                    "awaiting verified delivery"
                    if collected is not None
                    else "awaiting verified collection"
                ),
            )
            return {
                "collected_quantity": str(collected) if collected is not None else None,
                "needs_human": "handover_confirmation",
                "node_history": [*state.get("node_history", []), "await_handover"],
            }

        return {
            "collected_quantity": str(collected) if collected is not None else None,
            "delivered_quantity": str(delivered),
            "needs_human": None,
            "node_history": [*state.get("node_history", []), "await_handover"],
        }

    async def update_impact(state: FoodLoopState) -> dict[str, Any]:
        started = time.monotonic()
        await _event(state, "update_impact", EventPhase.ENTER)

        async with ctx.sessionmaker() as session:
            metrics, _row = await impact_service.compute_metrics(session)
            summary = await impact_service.narrate(session, metrics)

        await _event(
            state, "update_impact", EventPhase.EXIT,
            summary=f"impact updated: {state.get('delivered_quantity')} delivered",
            payload={"delivered": state.get("delivered_quantity")},
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        return {
            "impact_summary": summary,
            "node_history": [*state.get("node_history", []), "update_impact"],
        }

    async def close_workflow(state: FoodLoopState) -> dict[str, Any]:
        await _event(
            state, "close_workflow", EventPhase.EXIT,
            summary="workflow complete", to_state="COMPLETED",
        )
        async with ctx.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE workflow_runs SET status = 'COMPLETED', "
                    "completed_at = now() WHERE thread_id = :tid"
                ),
                {"tid": state["thread_id"]},
            )
            await session.commit()
        return {
            "terminal_reason": "completed",
            "node_history": [*state.get("node_history", []), "close_workflow"],
        }

    async def close_rejected(state: FoodLoopState) -> dict[str, Any]:
        await _event(
            state, "close_rejected", EventPhase.EXIT,
            summary=state.get("safety_summary") or "rejected on safety grounds",
            to_state="REJECTED",
        )
        return {
            "terminal_reason": "safety_rejected",
            "node_history": [*state.get("node_history", []), "close_rejected"],
        }

    async def no_recipient_hold(state: FoodLoopState) -> dict[str, Any]:
        await _event(
            state, "no_recipient_hold", EventPhase.EXIT,
            summary="no eligible recipient could take this donation",
            payload={"excluded": state.get("excluded")},
        )
        return {
            "terminal_reason": "no_recipient",
            "node_history": [*state.get("node_history", []), "no_recipient_hold"],
        }

    async def pending_info(state: FoodLoopState) -> dict[str, Any]:
        await _event(
            state, "pending_info", EventPhase.EXIT,
            summary="clarification rounds exhausted; parked pending donor input",
            payload={"missing_fields": state.get("missing_fields")},
        )
        return {
            "terminal_reason": "pending_info",
            "node_history": [*state.get("node_history", []), "pending_info"],
        }

    async def escalate_human(state: FoodLoopState) -> dict[str, Any]:
        await _event(
            state, "escalate_human", EventPhase.INTERRUPT,
            summary=state.get("last_error") or "escalated for manual review",
        )
        async with ctx.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE workflow_runs SET status = 'WAITING_HUMAN', "
                    "interrupt_reason = 'escalation', last_error = :err "
                    "WHERE thread_id = :tid"
                ),
                {"tid": state["thread_id"], "err": state.get("last_error")},
            )
            await session.commit()
        return {
            "needs_human": "escalation",
            "terminal_reason": "escalated",
            "node_history": [*state.get("node_history", []), "escalate_human"],
        }

    return {
        "extract_fields": extract_fields,
        "request_clarification": request_clarification,
        "validate_listing": validate_listing,
        "safety_evaluate": safety_evaluate,
        "human_safety_review": human_safety_review,
        "find_and_rank_recipients": find_and_rank_recipients,
        "reserve_quantity": reserve_quantity,
        "await_confirmation": await_confirmation,
        "assign_pickup": assign_pickup,
        "await_handover": await_handover,
        "update_impact": update_impact,
        "close_workflow": close_workflow,
        "close_rejected": close_rejected,
        "no_recipient_hold": no_recipient_hold,
        "pending_info": pending_info,
        "escalate_human": escalate_human,
    }


MANDATORY = (
    "title", "food_category", "unit", "quantity_total",
    "pickup_window_start", "pickup_window_end", "pickup_address_text",
)
