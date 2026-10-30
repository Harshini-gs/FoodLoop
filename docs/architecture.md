# FoodLoop — architecture

How the system is put together, and why each significant decision went the way
it did. Where a choice had a credible alternative, the alternative is named and
the trade-off stated.

---

## Layers

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Frontend — React + TypeScript + Vite                     │
│    login · donor · recipient · workflow · impact · admin    │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST, JWT bearer
┌───────────────────────────▼─────────────────────────────────┐
│ 2. API — FastAPI + Pydantic                                 │
│    validation · authn/authz · OpenAPI                       │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 3. Services — donation · matching · handover · impact       │
│    ┌─────────────────────────────────────────────────┐      │
│    │ safety rules engine — THE HARD GATE             │      │
│    └─────────────────────────────────────────────────┘      │
└──────────┬──────────────────────────────┬───────────────────┘
           │                              │
┌──────────▼──────────────┐   ┌───────────▼──────────────────┐
│ 4. Orchestration        │   │ Providers (leaf nodes)       │
│    LangGraph            │   │  llm · routing · notify      │
│    typed state          │   │  mock by default             │
│    checkpoints          │   │  NO database access          │
│    interrupts           │   └──────────────────────────────┘
└──────────┬──────────────┘
           │
┌──────────▼──────────────────────────────────────────────────┐
│ 5. Data — PostgreSQL 16 + PostGIS 3.4                       │
│    13 tables · CHECK constraints · safety trigger           │
│    scheduled_tasks (durable timers) · notifications (outbox)│
└─────────────────────────────────────────────────────────────┘
```

**The shape that matters:** providers are leaves. The LLM adapter hangs off the
service layer and has no edge to the database. It cannot read or write a row;
it receives text and returns text.

---

## Data flow for one donation

```
donor types free text
      │
      ▼
extract_fields ──────────► LLM provider (the ONE llm step)
      │                     returns DonationDraft, all fields Optional
      │
      ├─ missing fields? ──► request_clarification ──┐ max 2 rounds
      │                                              │
      ▼ ◄────────────────────────────────────────────┘
validate_listing
      │
      ▼
safety_evaluate ─────────► rules engine (deterministic, versioned)
      │                     writes safety_checks (append-only)
      │                     sets donations.safety_status
      │
      ├─ NEEDS_REVIEW ────► human_safety_review (no auto-resolve, ever)
      ├─ REJECTED ────────► close_rejected (terminal)
      ▼ ELIGIBLE
find_and_rank_recipients ► PostGIS ST_DWithin + 4-component score
      │                     writes matches with the score breakdown
      ▼
reserve_quantity ────────► SELECT … FOR UPDATE, insert, bump counter
      │                     trigger re-checks safety independently
      ├─ conflict ────────► next candidate, max 3
      ▼
await_confirmation ──────► interrupt + scheduled_tasks timer
      │                     outbox row written in the same transaction
      ▼
assign_pickup ───────────► nearest volunteer with capacity
      │                     routing provider stamps data_source
      ▼
await_handover ──────────► BLOCKED until a human confirms with a code
      │                     the graph cannot advance this itself
      ▼
update_impact ───────────► aggregates handover_events only
      ▼
close_workflow
```

---

## Decisions

### Row locking, not SERIALIZABLE

**Chosen:** `SELECT … FOR UPDATE` on the donation row, under READ COMMITTED.

**Alternative:** SERIALIZABLE isolation.

**Why:** both are correct. SERIALIZABLE makes *every* write in the application
a potential serialization failure needing a retry loop — a global cost to solve
contention on one row. The row lock is local to the place that needs it.

**Cost accepted:** the lock is held for the duration of the transaction, so
that transaction must stay short. No provider calls happen inside it.

### A `scheduled_tasks` table, not Redis + Celery

**Chosen:** a Postgres table swept with `FOR UPDATE SKIP LOCKED`.

**Why:** the timers must survive a restart. Redis without persistence
configured would be *less* reliable than the table, not more, and it is one
more service to run, configure and monitor.

**Cost accepted:** a few seconds of polling latency, irrelevant for a
fifteen-minute confirmation window. Celery is the documented upgrade path if
throughput ever demands it.

### Four quantity columns, not a computed sum

**Chosen:** `quantity_total / reserved / collected / delivered` denormalized on
`donations`, under one CHECK constraint.

**Alternative:** computing reserved quantity as a sum over reservation rows.

**Why:** the constraint can then express the whole invariant in one place, and
the hot read path is a single row rather than an aggregate.

**Cost accepted:** the counter must be updated in the same transaction as the
reservation. `python -m app.cli check` runs a drift check comparing the counter
against the sum of its rows.

### `geography(Point, 4326)`, not lat/lon floats

**Why:** `ST_DWithin` takes metres and uses a GIST index. A hand-rolled
Haversine in SQL cannot use an index, and gets slow exactly when the dataset
gets interesting.

**Cost accepted:** PostGIS becomes a hard dependency, which is why the database
is containerized.

### `timestamptz` everywhere, UTC in storage

**Why:** pickup windows cross local midnight. Comparing naive timestamps is a
bug waiting to happen, and one seeded recipient (Sahaya Night Shelter, open
18:00–02:00) exists specifically to exercise the wrap.

**Cost accepted:** every test must set an explicit timezone.

### Three-state rule results

**Chosen:** `passed: bool | None`, where `None` means the rule could not be
evaluated.

**Why:** a system that collapses `None` into `True` treats "we don't know when
this was cooked" as "this was cooked recently." The resolution order makes any
unevaluated rule force `NEEDS_REVIEW`.

**Cost accepted:** more donations land in human review than a two-state system
would produce. That is the correct direction to be wrong in.

### Pure router functions

**Chosen:** every branch decision is a function of state alone, in
`app/workflows/routers.py`, with no LangGraph import.

**Why:** they unit-test in milliseconds with hand-written dicts, and a
LangGraph version bump touches only `graph.py`. Given how much that library's
API has moved, the isolation is a deliberate hedge.

### `data_source` NOT NULL with no default

**Why:** honesty enforced by the schema rather than by a UI label someone
forgets to add. A row carrying a number cannot exist without declaring whether
that number is real, cached, simulated or estimated.

**Cost accepted:** every insert must set it. That is the point.

---

## The state machines

Four, not one. A single machine would mix concerns that genuinely differ: a
donation can be partly reserved while one pickup is collected and another is
still scheduled.

```
DONATION   DRAFT → SAFETY_REVIEW → ELIGIBLE → PARTIALLY_RESERVED
                 → FULLY_RESERVED → IN_TRANSIT → COMPLETED
           terminal: PENDING_INFO, REJECTED, CANCELLED, EXPIRED,
                     NEEDS_MANUAL_REVIEW

MATCH      PROPOSED → AWAITING_CONFIRMATION → ACCEPTED
                    | DECLINED | EXPIRED | CANCELLED

RESERVATION  HELD → CONFIRMED → CONSUMED
                  | RELEASED                  ← the only mover of quantity

PICKUP     SCHEDULED → EN_ROUTE → COLLECTED → DELIVERED
                     | PARTIAL | FAILED | CANCELLED
           COLLECTED and DELIVERED require a handover_events row
```

Donation status is **derived** from its reservations rather than set
independently, which removes a class of inconsistency where the two disagree.

---

## Tables

| Group | Tables |
|---|---|
| Identity | `users`, `organizations` |
| Donation | `donations`, `safety_rule_versions`, `safety_checks` |
| Fulfilment | `matches`, `reservations`, `pickups`, `handover_events` |
| Operations | `workflow_runs`, `workflow_events`, `notifications`, `scheduled_tasks` |

Volunteer details fold into `users`; recipient capabilities fold into
`organizations`. Both were separate tables in the original design and were
merged when the scope was cut — a user belongs to at most one organization, so
the join table earned nothing.

**Append-only:** `safety_checks`, `handover_events`, `workflow_events`. A
correction inserts a new row rather than editing one, so the decision history
survives.

---

## Where the LLM is, and is not

**Is:**
- `app/providers/llm.py` → `extract_donation` (free text → structured draft)
- `app/providers/llm.py` → `summarize_impact` (prose over finished numbers)

**Is not:** anywhere else. The interface has two methods, and that is the
entire surface of the model's authority in this system.

The enforcement is not a policy document:

1. `app/agents/permissions.py` grants each node a frozen set of tool names; a
   decorator refuses anything else and fails closed with no node context.
2. `record_handover`, `set_safety_status` and `resolve_safety_review` are in a
   `NEVER_GRANTED` set, asserted by `tests/unit/test_permissions.py`.
3. The database trigger refuses a reservation against a non-`ELIGIBLE`
   donation, including from raw SQL.
4. `SafetyDecision` is frozen, so a computed verdict cannot be rewritten.

---

## Observability

`workflow_events` carries an ENTER and EXIT row per node, with a summary, a
structured payload and a duration. It is the sole source for the frontend's
workflow view — the UI renders rows that exist rather than an animation, which
is the difference between a workflow visualization and a decoration.

`notifications` is an outbox: the row is written in the same transaction as the
state change that caused it, and delivery is retried independently by the
sweeper. Without that, sending inside the transaction means a provider timeout
rolls back the reservation, and sending outside it means a crash loses the
notification while keeping the reservation.

Logging redacts passwords, tokens and handover codes through
`app/core/logging.py:REDACT_KEYS`, and handover codes are additionally never
passed to the logger in the first place.
