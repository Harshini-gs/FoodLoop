# FoodLoop — interview guide

Read this before you show the project to anyone. It is not a script to
memorise: a memorised answer collapses on the second question, and the second
question is always "why?"

Work through it with the code open. Each section names the file so you can
look at the thing being described. **If you cannot find the file, you cannot
defend the claim — go and look.**

---

## The one-sentence version

> FoodLoop coordinates surplus food donations. A language model does exactly
> two jobs — reading messy donor text into structure, and writing prose over
> numbers computed elsewhere — and it is structurally incapable of clearing a
> safety check, moving quantity, or closing a donation.

That second clause is the project. Lead with it.

---

## 1. Why LangGraph and not a loop with tool calls?

**Short answer:** because the workflow pauses for humans and has to survive
restarts.

**The reasoning.** Two steps in this workflow wait on a person: a safety
review, and a recipient confirming an offer. Confirmation has a fifteen-minute
window. If the state lived in memory, restarting the server would strand every
donation mid-flight — the hold would still exist in the database, but nothing
would remember what to do next.

LangGraph persists a checkpoint after every node, so a resumed run continues
from where it stopped.

**Files:** `app/workflows/graph.py` (construction and the checkpointer),
`app/api/v1/workflow.py` (`/resume`).

**The follow-up you will get:** *"Couldn't you just store the state yourself?"*
Yes, and that is a fair challenge — the answer is that you would be
reimplementing checkpointing, interrupts and resumption, which is what the
library already does. The honest counterpoint is that for a workflow this size
it is a close call.

**What to be honest about:** the graph construction is isolated in one file on
purpose, because LangGraph's API has churned across versions. All branch logic
lives in `routers.py` as pure functions that do not import LangGraph at all —
so an upgrade touches one file and the branch tests keep passing.

---

## 2. What makes these agents rather than functions?

**Short answer:** two of them are genuinely agentic. The rest are deterministic
services exposed as graph nodes, and calling them agents would be inflation.

| Component | What it actually is |
|---|---|
| Surplus | LLM extraction + deterministic validation — **agentic** |
| Matching | PostGIS query + weighted arithmetic — deterministic |
| Safety | Versioned rules engine — deterministic, deliberately |
| Logistics | Nearest-feasible volunteer — deterministic |
| Coordination | State machine + durable timers — **agentic** in that it selects among permitted next steps |
| Impact | SQL aggregation + LLM prose over finished numbers — mostly deterministic |

**Say this plainly:** "I did not make safety an agent because a rules engine is
the correct tool. An LLM asked whether food is safe will give you a confident
answer with no audit trail, and you cannot version it, test it, or explain it
to a regulator."

That answer is better than claiming six autonomous agents. Interviewers have
seen plenty of projects that wrap six LLM calls and call it a multi-agent
system.

---

## 3. How do you stop two recipients claiming the same food?

**This is the question you will definitely be asked.** It is the most
technically interesting thing in the project.

**The problem.** Two NGOs tap "claim" on the last 30 meals in the same
half-second. Under READ COMMITTED with no lock, both transactions read
`remaining = 30`, both pass their check, both insert. Sixty meals promised,
thirty exist, and one volunteer drives across Bengaluru for nothing.

**The fix** (`app/repositories/reservations.py`):

```
BEGIN
  SELECT quantity_total, quantity_reserved, safety_status
    FROM donations WHERE id = :id FOR UPDATE;   -- contenders queue HERE
  -- assert against values read UNDER the lock
  INSERT INTO reservations (...);
  UPDATE donations SET quantity_reserved = quantity_reserved + :qty;
COMMIT
```

The row lock serializes the two transactions. The second wakes up after the
first commits, re-reads, finds nothing remaining, and rolls back to a clean
409.

**Why not SERIALIZABLE?** It would also be correct. It is rejected because it
makes *every* write in the application a potential serialization failure
needing a retry loop — a global cost to solve contention on one row. A row
lock is local to the place that needs it.

**Have this ready, because it is the real follow-up.**

**Defence in depth.** Three more mechanisms guard the same invariant, and each
would catch a bug in the others:

- `CHECK (quantity_reserved <= quantity_total AND quantity_collected <= quantity_reserved AND ...)` — the arithmetic cannot oversell even if the Python is wrong
- a partial unique index on `(donation_id, recipient_org_id) WHERE status IN ('HELD','CONFIRMED')` — a replayed event cannot create a second hold
- an idempotency key — a retried HTTP request returns the original reservation

**The test:** `tests/integration/test_concurrency.py`. Two *real* database
sessions, fired with `asyncio.gather`, asserting exactly one 201 and one 409.
One session used twice would be serialized by the client and prove nothing.

---

## 4. Why can't the LLM approve unsafe food?

**Short answer:** it has no tool that could, and the database would refuse
anyway.

Four independent layers:

1. **No tool exists.** `app/agents/permissions.py` maps each node to a frozen
   set of permitted tool names. A decorator refuses anything else at call
   time, and fails closed when there is no node context.
2. **`record_handover` is granted to nobody.** It is in a `NEVER_GRANTED` set,
   and `tests/unit/test_permissions.py` asserts that stays true. That test is
   what keeps the guarantee alive after someone adds a tool in six months.
3. **A database trigger.** `assert_safety_cleared()` fires `BEFORE INSERT ON
   reservations` and raises unless the donation's `safety_status` is
   `ELIGIBLE`. This holds against raw SQL, a compromised prompt, or a bug in
   the service layer.
4. **The verdict is immutable.** `SafetyDecision` is a frozen dataclass of
   frozen `RuleResult`s. An agent holding one cannot rewrite `outcome`.

**Say this:** "The guarantee is structural, not instructional. I did not tell
the model to behave — I never gave it the capability."

Then show them the trigger in
`migrations/versions/0001_initial_schema.py`. It lands better than any
explanation.

---

## 5. What happens when evidence is missing?

**This is the subtlest good idea in the project.** Make sure you understand it.

A rule has three outcomes, not two (`app/services/safety/checks.py`):

```python
passed: bool | None    # True = satisfied, False = violated, None = UNEVALUABLE
```

If a donation has no `prepared_at`, the cooked-food window rule cannot be
evaluated at all. It returns `None`, and any unevaluated rule forces
`NEEDS_REVIEW`.

**Why it matters:** a system that collapses `None` into `True` treats "we don't
know when this was cooked" as "this was cooked recently". That is how people
get sick.

Resolution order, in `app/services/safety/engine.py`:

```
1. any HARD rule FAILED      -> REJECTED
2. any rule UNEVALUATED      -> NEEDS_REVIEW
3. any SOFT rule FAILED      -> NEEDS_REVIEW
4. otherwise                 -> ELIGIBLE
```

Hard failure beats missing evidence deliberately: if the food is demonstrably
nine hours old, an unrelated absent field should not downgrade a rejection
into a review.

**The seed data proves it.** One fixture has a null `prepared_at` specifically
so a regression here fails a test.

---

## 6. How do you avoid infinite loops?

**Short answer:** every cycle has a counter in the workflow state compared
against a configured cap. There are no recursion limits.

```python
# app/workflows/routers.py
rounds = attempts_for(state, ATTEMPT_CLARIFICATION)
if rounds >= settings.max_clarification_rounds:
    return "pending_info"
```

Three bounded cycles: clarification (2 rounds), candidate attempts (3), search
widening (2).

**Say this:** "A recursion limit is a crash, not a design. It tells you a loop
ran away but not which one, and the user gets a 500 instead of a sensible
outcome."

One subtlety worth knowing: `merge_attempts` in `state.py` takes the **max**
when merging state updates. Without that reducer, two nodes updating `attempts`
could clobber each other, a counter could silently reset, and a bounded loop
would become infinite. There is a test for it.

---

## 7. Why no Redis or Celery?

**Short answer:** the timers must survive a restart, and a Postgres table
already does that.

`scheduled_tasks` rows are claimed with `FOR UPDATE SKIP LOCKED`
(`app/tasks/sweeper.py`). Durable, no extra infrastructure, and several
workers can run safely. The cost is a few seconds of polling latency, which is
irrelevant for a fifteen-minute window.

**Say this:** "Redis without persistence configured would be *less* reliable
than the table, not more. Celery is the documented upgrade path if throughput
demands it — I removed infrastructure deliberately rather than adding it
because a tutorial did."

Being able to explain a removal is a stronger signal than listing
technologies.

---

## 8. How do you know your impact numbers are real?

**Short answer:** they come from `handover_events`, which require a verified
code and an authenticated actor.

- `quantity_offered` is what donors listed
- `quantity_delivered` sums `handover_events WHERE kind = 'DELIVER'`
- they are different columns from different tables and the report shows both

**The honesty mechanisms:**

- `data_source` is an enum (`REAL` / `CACHED` / `SIMULATED` / `ESTIMATED`),
  **NOT NULL with no default**, on every row that can carry an estimate. A row
  cannot exist without declaring what it is. The frontend reads the column.
- a rate with a zero denominator returns `null`, never 0. A 0% recovery rate
  over zero donations is not a measurement.
- every metric carries its formula, denominator, inclusion criteria and source
  tables (`/api/v1/impact/definitions`).
- `handover_events` has `CHECK (code_verified)` — an unverified handover is
  unstorable.

**Say this:** "Counting what was offered as what was recovered is the standard
way food-waste projects produce impressive numbers that mean nothing. The
schema makes it impossible here."

**What is NOT implemented, and say so:** no environmental impact coefficient.
Converting food weight to CO2e needs a methodology this project does not have,
and inventing one would make the number worse than useless.

---

## 9. What is simulated, and why is that acceptable?

In the default configuration: LLM extraction (regex-based mock), route
estimates (haversine × a road factor), and notifications (logged to console).

Each sits behind an interface in `app/providers/` with a real implementation
slot. The workflow logic is identical either way.

**Two things make this defensible rather than a cop-out:**

1. Everything simulated is labelled — in the database column, in the API
   response, and in the UI.
2. The unimplemented providers **raise** rather than silently falling back to
   the mock. A silent fallback would mean the app claims to use a real model
   while running regex.

**Say this:** "Every test runs offline with no API key, which means the test
suite is deterministic and free. That is a feature, not a limitation."

---

## 10. What would you do with more time, and what is weakest?

Have an honest answer. Interviewers trust someone who knows their own gaps.

**Not built, by choice:**

- OR-Tools multi-stop route optimization under capacity and time windows.
  Currently it is nearest-feasible-volunteer, which is correct but not
  optimal.
- Demand forecasting. Needs rolling-origin validation against a seasonal-naive
  baseline, and on synthetic data it would prove nothing. **A forecasting
  module with no honest evaluation is worse than none.**
- A disruption simulator and an evaluation suite with baseline comparisons.
- Two of the four designed failure branches (the clarification loop and the
  confirmation timeout are designed and documented; safety review and
  reservation conflict are built).

**Genuinely weakest parts, if asked:**

- The mock extractor is regex and will mis-parse plenty of real phrasing. That
  is acceptable because unparsed fields become `missing_fields` rather than
  wrong values — the failure mode is a clarifying question, not a bad listing.
- Quantities mix units (meals, boxes, kg, litres) and are not converted,
  because there is no honest conversion between a meal and a kilogram. It
  makes aggregate figures approximate, and the report says so.
- The safety thresholds are placeholders. They are labelled
  `ILLUSTRATIVE_DEMO` and the engine refuses to load a ruleset without a
  policy status, but they are not real limits and must not be presented as
  any.

---

## The question that matters most

> **"What was the hardest part?"**

Do not say "learning LangGraph."

Say something like:

> "Realising the interesting problem wasn't the AI. It was making sure the AI
> couldn't touch the four things that actually matter — the safety decision,
> the quantity, the handover, and the metrics. Most of the engineering went
> into building constraints the model cannot route around, rather than into
> prompting."

Then show them the trigger.

---

## Before you present this

Work through these honestly. Each one is a claim you would be making.

- [ ] Open `app/repositories/reservations.py` and explain the five statements
      in your own words, without reading the comments.
- [ ] Open `tests/integration/test_concurrency.py` and explain why two
      sessions matter and one would not.
- [ ] Find the safety trigger in the migration and explain what it refuses.
- [ ] Run `pytest` and know what the number of passing tests actually covers.
- [ ] Explain why `passed` is `bool | None` and not `bool`.
- [ ] Explain why `data_source` has no default.
- [ ] Find one thing in this codebase you think is wrong or over-engineered,
      and be ready to say so. Nothing builds credibility faster than
      criticising your own work accurately.

**A warning worth taking seriously.** If you present this as entirely your own
work and cannot answer a follow-up, that costs you more than a smaller project
you built yourself would have. If you are asked how you built it, "I designed
the architecture and worked through the implementation with AI assistance, and
here is the part I had to understand most deeply" is a respectable answer in
2026. Pretending otherwise is not, and it is easy to detect.
