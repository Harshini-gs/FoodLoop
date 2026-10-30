# The four mechanisms you build yourself

These are deliberately **not** implemented in this scaffold.

They are the parts an interviewer will ask you about, so you need to have
written them. Each spec below is precise enough to implement without guessing,
and each names the test that proves it works.

Build them with Claude Code running locally in this repo, so the tests actually
execute. Read each spec first, then implement, then run the test.

---

## 1. The reservation transaction

**File:** `backend/app/repositories/reservations.py`
**Test:** `backend/tests/integration/test_concurrency.py`
**Day:** 3

### What it must do

Reserve `quantity` of a donation for a recipient organization, such that two
simultaneous requests for the last available units result in exactly one
success and one `409 Conflict`.

### The algorithm

```
BEGIN
  -- 1. Take a row lock. This is the whole mechanism: contenders queue here.
  SELECT quantity_total, quantity_reserved, safety_status
    FROM donations
   WHERE id = :donation_id
     FOR UPDATE;

  -- 2. Assert, in application code, with the row we now hold exclusively:
  --      safety_status == 'ELIGIBLE'            -> else SafetyGateViolation
  --      total - reserved >= requested          -> else ReservationConflict
  --
  --    Re-read matters. Any value read BEFORE the lock is stale.

  -- 3. Insert the reservation. The BEFORE INSERT trigger independently
  --    re-checks safety; the partial unique index independently rejects a
  --    second live hold for the same recipient.
  INSERT INTO reservations (...) VALUES (...);

  -- 4. Bump the denormalized counter. The CHECK constraint is the backstop
  --    if steps 2-3 were somehow wrong.
  UPDATE donations SET quantity_reserved = quantity_reserved + :qty
   WHERE id = :donation_id;
COMMIT
```

### Why `FOR UPDATE` and not `SERIALIZABLE`

Under READ COMMITTED without a lock, both transactions read `remaining = 30`,
both pass the check, both insert. The CHECK constraint catches it — but as a
500-level database error, not a clean 409, and only because the constraint
exists.

SERIALIZABLE would also be correct, but it makes *every* write in the
application a potential serialization failure needing a retry loop. For a
single contended row, a row lock is the cheaper correct answer.

**Have this distinction ready. It is the most likely follow-up question.**

### Idempotency

If `idempotency_key` is supplied and a reservation already exists with that
key, return the existing reservation and do not insert. A retried HTTP request
must not create a second hold. The unique constraint on the column is the
enforcement; the lookup is the optimization.

### The test that proves it

```python
async def test_concurrent_reservations_exactly_one_wins(session, raw_connection):
    # donation with exactly 30 remaining
    # launch BOTH reservations with asyncio.gather on TWO REAL SESSIONS
    # assert: exactly one succeeded
    # assert: the other raised ReservationConflict
    # assert: quantity_reserved == quantity_total   (no overselling)
    # assert: exactly one reservation row exists
```

Two real connections. Not mocks, not one session used twice — the point is
that the database serialized them.

---

## 2. The safety rules engine

**File:** `backend/app/services/safety/engine.py`
**Test:** `backend/tests/unit/test_safety_engine.py`
**Day:** 2

### What it must do

Load `rulesets/demo_v1.yaml`, evaluate every applicable rule against a
donation, and return one of `ELIGIBLE` / `NEEDS_REVIEW` / `REJECTED` with a
per-rule breakdown.

### Hard requirements

1. **Refuse to load a ruleset with no `policy_status` key.** Raise at load
   time. This is what keeps a demo policy distinguishable from a verified one.

2. **Missing evidence never passes.** If a rule's `requires` fields are NULL,
   the rule is UNEVALUATED and the donation goes to `NEEDS_REVIEW` (or
   `REJECTED` if `on_missing_evidence: reject`). There must be no code path
   where a NULL becomes a pass.

3. **Outcome resolution, in this exact order:**
   ```
   any hard rule FAILED        -> REJECTED
   any rule UNEVALUATED        -> NEEDS_REVIEW
   any soft rule FAILED        -> NEEDS_REVIEW
   otherwise                   -> ELIGIBLE
   ```
   Hard failure wins over everything. A hard failure is not appealable.

4. **Every result is recorded** in `safety_checks.per_rule` as a list of
   `{rule_id, severity, passed, reason, observed, limit}`. `passed` is `None`
   for unevaluated. This is what the UI renders as "why was this rejected".

5. **Pin the rule version.** The `safety_checks` row references the
   `safety_rule_versions.id` that produced the decision.

### Check types to implement

`timestamp_after_now`, `timestamp_order`, `numeric_min`, `field_present`,
`flag_is_true`, `text_min_length`, `hours_between_at_most`,
`hours_between_at_least`, `conditional_flag_required`, `category_in_list`,
`window_overlaps_hours`.

Each is a small pure function `(donation_dict, rule_config) -> RuleResult`.
Keep them pure — that is what makes them trivially unit-testable.

### The timezone trap

`window_overlaps_hours` compares a `timestamptz` pickup window against
`opens_at`/`closes_at`, which are local wall-clock times. Convert the window to
`DEFAULT_TIMEZONE` first. And handle the wrap: Sahaya Night Shelter opens 18:00
and closes 02:00, so `closes_at < opens_at` means the interval crosses
midnight. The seed data includes that fixture on purpose.

### Tests to write

- Each check type, pass and fail.
- NULL `prepared_at` on cooked food -> `NEEDS_REVIEW`, never `ELIGIBLE`.
- A hard failure plus a soft pass -> `REJECTED`.
- An unevaluated rule plus all passes -> `NEEDS_REVIEW`.
- Loading a ruleset with `policy_status` removed -> raises.
- The midnight-crossing window overlaps correctly.
- **`test_llm_cannot_override_hard_failure`**: construct a result with a hard
  failure, attempt to override the outcome through the public API, assert it is
  impossible. Make the engine's return value immutable so this is structural.

---

## 3. The matching score

**File:** `backend/app/services/matching.py`
**Test:** `backend/tests/unit/test_scoring.py`
**Day:** 3

### Stage 1 — deterministic filter (SQL)

```sql
SELECT * FROM organizations
WHERE kind = 'RECIPIENT'
  AND is_active AND is_verified
  AND ST_DWithin(location, :donor_point, :radius_m)
  AND :food_category = ANY(accepted_categories)
  AND (NOT :needs_refrigeration OR has_refrigeration)
```

Every excluded candidate is recorded with a human-readable reason, so a donor
can see why a nearby NGO was not offered the food. Exclusions are data, not
silence.

### Stage 2 — weighted score (Python, from DB values only)

| Component | Weight | Formula |
|---|---|---|
| proximity | 0.30 | `1 - min(distance_m / radius_m, 1)` |
| capacity_fit | 0.25 | see below |
| window_slack | 0.20 | `min(overlap_minutes / 60, 1)` |
| reliability | 0.25 | Wilson lower bound on `offers_accepted / offers_received` |

**capacity_fit** rewards a good match in both directions. A recipient who can
take exactly the offered amount scores 1.0; one who can take far more is
slightly penalized (the food could have gone somewhere it was needed more):

```
ratio = min(donation_qty, daily_capacity) / donation_qty    # how much fits
waste = max(0, (daily_capacity - donation_qty) / daily_capacity) * 0.2
capacity_fit = max(0, ratio - waste)
```

**reliability** uses the Wilson score lower bound, not a raw average, so an
organization with 1 of 1 accepted does not outrank one with 47 of 50:

```
n = offers_received
if n == 0: return 0.5            # no history: neutral prior, NOT zero
p = offers_accepted / n
z = 1.96
denom = 1 + z²/n
centre = p + z²/(2n)
margin = z * sqrt(p(1-p)/n + z²/(4n²))
return (centre - margin) / denom
```

Vatsalya Elder Care in the seed has zero history precisely to exercise the
`n == 0` branch. Division by zero there is the bug this fixture catches.

### Hard requirements

- No LLM call anywhere in this file. Distances come from PostGIS, capacity and
  history from table columns.
- Store the full breakdown in `matches.score_components`, with the raw values
  each component came from.
- Weights sum to 1.0 — assert it at import time.

### Tests

- Each component in isolation, including boundary values.
- Wilson with `n = 0`, `n = 1`, `n = 50`.
- Known candidate set -> known ranking order.
- `capacity_fit` is lower for a 500-capacity recipient on a 10-meal donation
  than for a 60-capacity one.

---

## 4. The LangGraph workflow

**Files:** `backend/app/workflows/state.py`, `graph.py`, `routers.py`
**Tests:** `backend/tests/integration/test_workflow_*.py`
**Day:** 4

### State

```python
class FoodLoopState(TypedDict):
    donation_id: str
    run_id: str
    draft: dict | None              # DonationDraft, serialized
    safety_outcome: str | None
    candidates: list[dict]
    candidate_index: int            # which ranked candidate we are trying
    reservation_id: str | None
    pickup_id: str | None
    attempts: dict[str, int]        # EVERY loop bound lives here
    last_error: str | None
    needs_human: str | None         # interrupt reason
```

### Nodes (9)

`ingest_donation` -> `extract_fields` -> `validate_listing` ->
`safety_evaluate` -> `find_and_rank_recipients` -> `reserve_quantity` ->
`await_confirmation` -> `verified_handover` -> `update_impact`

### Branch functions — keep these PURE

Put every routing decision in `routers.py` as a function of state alone:

```python
def route_after_safety(state: FoodLoopState) -> str:
    outcome = state["safety_outcome"]
    if outcome == "ELIGIBLE":     return "find_and_rank_recipients"
    if outcome == "NEEDS_REVIEW": return "human_safety_review"
    return "close_rejected"
```

Pure functions test without a graph, a database or an LLM. This is what makes
"every branch is tested independently" cheap rather than painful. It is also
what makes a LangGraph version bump touch one file.

### The four failure branches

| Branch | Condition | Bound | On exhaustion |
|---|---|---|---|
| clarification | `draft.missing_fields` non-empty | `MAX_CLARIFICATION_ROUNDS` (2) | `PENDING_INFO` |
| safety review | outcome `NEEDS_REVIEW` | no auto-retry, ever | stays interrupted |
| reservation conflict | `ReservationConflict` raised | `MAX_CANDIDATE_ATTEMPTS` (3) | `no_recipient_hold` |
| confirmation timeout | timer fires | shares the candidate budget | `no_recipient_hold` |

Every back-edge increments `state["attempts"][key]` and compares against the
cap from settings. **No recursion limits** — a recursion limit is a crash, not
a design.

### Checkpointing

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# thread_id = f"donation:{donation_id}"
```

### Idempotency

Every side-effecting tool takes a key derived from
`(run_id, node_name, attempt)`. A replayed node must be a no-op, not a second
reservation or a second notification.

### Tests

- `test_routers.py` — every branch function, pure, no fixtures.
- `test_workflow_happy_path.py` — end to end on seed data.
- `test_workflow_safety_review.py` — interrupts, waits, resumes after a human.
- `test_workflow_resume.py` — **the important one.** Start a run, kill it
  mid-flight, restart, assert it resumes from the checkpoint and that no side
  effect happened twice (exactly one reservation row, exactly one
  notification).
- `test_permissions.py` — assert no node is granted `record_handover`.

### The permission registry

Already specified in the sprint plan. `app/agents/permissions.py` maps node
name to allowed tool names, a decorator enforces it at call time, and
**nothing anywhere is granted `record_handover`** — handover is written only by
an authenticated API call carrying a verified code.

---

## Order of work

1. Safety engine (Day 2) — pure functions, no database, easiest start.
2. Reservation transaction (Day 3) — the highest-value hour of the week.
3. Matching score (Day 3) — pure functions again, fast once scoring is clear.
4. Workflow (Day 4) — needs all three of the above to exist.

Do not start the workflow before the services work. The graph is orchestration;
it has nothing to orchestrate until the services beneath it run.
