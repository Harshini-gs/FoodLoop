# FoodLoop

An agentic AI platform for coordinating surplus food donations: free-text
intake, a deterministic food-safety gate, geospatial recipient matching,
transactional reservations, verified handover, and impact measured from what
actually arrived.

---

## ⚠️ Read this first — the safety rules are illustrative

**The food-safety thresholds in `demo_v1.yaml` are placeholders written for a
demonstration. They have not been reviewed by any food safety authority and
must not be used to decide whether real food is safe to eat.**

The engine refuses to load a ruleset that does not declare its policy status,
so this cannot be forgotten quietly: every safety decision in the system
carries the version of the ruleset that produced it, and that version is
labelled `ILLUSTRATIVE_DEMO — NOT VERIFIED AGAINST ANY FOOD SAFETY AUTHORITY`.

Replacing the thresholds with reviewed ones is a config change plus a new
version row. Getting them reviewed is the part that needs qualified people.

## Status

- **95 tests pass** — 65 unit, 30 integration against a live PostgreSQL 16 +
  PostGIS 3.4 container. `cd backend && uv run pytest -q`.
- **The full loop has been driven end to end** in the browser: a donation
  listed, cleared by the safety engine, claimed by a recipient, a volunteer
  assigned, 117 of 120 meals collected, 117 delivered against a verified code,
  and the Impact dashboard reporting 117 delivered from `handover_events`.
- **Extraction runs against a real language model** — a local one, through
  Ollama, with no API key and no network dependency. `LLM_PROVIDER=mock` is
  the default and is what the test suite uses; `anthropic` is implemented too.
- **The orchestrator runs on the sequential engine.** LangGraph installs and
  its API matches, but `langgraph-checkpoint-postgres` 3.x hangs during
  checkpointer setup against this schema. Graph construction is isolated in
  `app/workflows/graph.py` precisely so this could be routed around:
  `run_sequential` walks the same nodes and the same pure routers, losing
  checkpointing and nothing else. Both engines are selectable in the UI and
  the response states which one actually ran.

### Known gaps, stated plainly

- No frontend tests, and no tests that drive the HTTP layer through a client.
  Both of the real bugs found by hand lived in exactly that gap.
- The delivery code is stored in plaintext in `handover_events.notes`. The API
  never returns it, but it should be hashed like the collection code.
- No coverage measurement, so this README quotes test counts and never a
  percentage.

---

## What it does

A restaurant types *"50 extra veg meal boxes, pickup between 8 and 9 PM."*
FoodLoop then:

1. **Extracts** structured fields from that sentence — the one LLM step.
2. **Asks** about anything missing rather than guessing. A fabricated
   preparation time would walk straight through the safety gate.
3. **Evaluates** food safety against a versioned rules engine. Three outcomes:
   `ELIGIBLE`, `NEEDS_REVIEW`, `REJECTED`. Missing evidence lands in review,
   never in eligible.
4. **Finds** recipients within 8 km using PostGIS, filtering on category,
   capacity, refrigeration and opening hours, and records why each exclusion
   happened.
5. **Ranks** them on four components — proximity, capacity fit, window slack,
   and a Wilson lower bound on acceptance history — storing the breakdown so
   every recommendation is explainable.
6. **Reserves** quantity inside a transaction that two simultaneous claims
   cannot both win.
7. **Assigns** the nearest volunteer whose vehicle can carry the load.
8. **Waits** for a human to confirm, with a durable timer that survives a
   restart.
9. **Records** handover against a confirmation code — the actual quantity, not
   the offered one.
10. **Reports** impact from those verified records only.

---

## The claim worth checking

> The language model does exactly two things: reads messy donor text into
> structure, and writes prose over numbers computed elsewhere. It is
> structurally incapable of clearing a safety check, moving quantity, or
> closing a donation.

Four independent mechanisms, each of which would catch a failure in the others:

| Mechanism | Where | What it refuses |
|---|---|---|
| Tool permission registry | `app/agents/permissions.py` | Any tool a node was not granted. `record_handover` is granted to nobody. |
| Database trigger | `migrations/versions/0001_initial_schema.py` | A reservation against a donation that is not `ELIGIBLE` — even from raw SQL. |
| CHECK constraints | same migration | Overselling quantity; a handover with an unverified code. |
| Immutable verdict | `app/services/safety/engine.py` | Rewriting a safety outcome after it is computed. |

The tests that prove it: `tests/unit/test_permissions.py`,
`tests/integration/test_safety_gate.py`,
`tests/integration/test_constraints.py`.

---

## The concurrency problem

Two NGOs claim the last 30 meals in the same half-second. Without a lock, both
read `remaining = 30`, both pass their check, both insert.

`app/repositories/reservations.py`:

```sql
BEGIN
  SELECT quantity_total, quantity_reserved, safety_status
    FROM donations WHERE id = :id FOR UPDATE;   -- contenders queue here
  -- assert against values read UNDER the lock
  INSERT INTO reservations (...);
  UPDATE donations SET quantity_reserved = quantity_reserved + :qty;
COMMIT
```

SERIALIZABLE would also be correct. It is rejected because it makes every
write in the application a potential serialization failure needing a retry
loop, to solve contention on a single row.

`tests/integration/test_concurrency.py` fires two **real** database sessions
with `asyncio.gather` and asserts exactly one 201 and one 409. One session used
twice would be serialized by the client and prove nothing.

---

## Setup

Requires **Docker** and **Python 3.12** (not 3.13 — narrower wheel coverage for
the geo and database packages).

### 1. Database

```bash
docker compose up -d db
docker compose ps                         # wait for "healthy"
docker exec foodloop-db psql -U foodloop -d foodloop -c "SELECT postgis_version();"
```

That last command must print a version. Nothing downstream works without it.

### 2. Python environment

```bash
cd backend
uv python install 3.12
uv sync                                   # the first real checkpoint
uv sync --extra agents                    # adds LangGraph; optional
```

LangGraph is a separate extra on purpose: a resolution problem there should not
block the first three days of work. Without it, the workflow falls back to a
sequential runner using the same nodes and routers — no checkpointing.

### 3. Configuration

```bash
cp ../.env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste that into `.env` as `JWT_SECRET`. Every other default works offline.

### 4. Migrations and seed

```bash
uv run alembic upgrade head
uv run alembic downgrade base && uv run alembic upgrade head   # proves reversibility
uv run python -m app.cli seed --demo
uv run python -m app.cli check                                 # row counts + invariant check
```

### 5. Run

```bash
uv run uvicorn app.main:app --reload
```

- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

```bash
cd ../frontend
npm install
npm run dev                               # http://localhost:5173
```

Demo password for every seeded account: `foodloop-demo-2026`

| Role | Email |
|---|---|
| Donor | `staff@nandini.veg.restaurant.example` |
| Recipient | `staff@annapurna.community.kitchen.example` |
| Admin | `admin@foodloop.example` |

### 6. Tests

```bash
cd backend
uv run pytest tests/unit -q               # no database needed

docker exec foodloop-db psql -U foodloop -d foodloop -c "CREATE DATABASE foodloop_test;"
export DATABASE_URL_SYNC="postgresql+psycopg://foodloop:foodloop@localhost:5432/foodloop_test"
uv run alembic upgrade head
unset DATABASE_URL_SYNC
uv run pytest -q
```

On Windows PowerShell, replace the `export`/`unset` lines with
`$env:DATABASE_URL_SYNC="..."` and open a fresh terminal afterwards.

Integration tests skip with a clear reason when no database is reachable, so
`pytest` never fails for the wrong cause.

---

## Real, simulated, estimated

Every external dependency has a deterministic offline implementation, and it is
the default. The whole workflow runs with no credentials and no network.

| Provider | Default | What it does |
|---|---|---|
| LLM | `mock` | Regex extraction. Deterministic, offline. |
| Routing | `simulated` | Haversine × road factor. **Not navigation.** |
| Geocoding | `simulated` | Seed data ships with real coordinates. |
| Notifications | `console` | Writes an outbox row and logs it. |

The unimplemented real providers **raise** rather than falling back to the
mock. A silent fallback would mean the application claims to use a real model
while running regex.

Every row that can carry an estimate has a `data_source` column
(`REAL` / `CACHED` / `SIMULATED` / `ESTIMATED`) that is `NOT NULL` **with no
default**. A row cannot exist without declaring what it is, and the frontend
displays it.

---

## ⚠️ Food safety

`app/services/safety/rulesets/demo_v1.yaml` contains **placeholder thresholds
only**:

```yaml
policy_status: "ILLUSTRATIVE_DEMO — NOT VERIFIED AGAINST ANY FOOD SAFETY AUTHORITY"
```

Nothing in it comes from FSSAI, Codex Alimentarius, HACCP or any other
authority, and no food-safety professional has reviewed it. The limits exist to
make the workflow demonstrable.

The engine **refuses to load a ruleset with no `policy_status` key**, so a demo
policy cannot be quietly mistaken for a verified one. Real deployment needs
real thresholds from a qualified source, reviewed by qualified people —
swapping them in is a config change plus a new `safety_rule_versions` row, with
no code change.

---

## What is NOT implemented

Stated plainly rather than left for someone to discover:

- **OR-Tools multi-stop route optimization.** Volunteer assignment is
  nearest-feasible — correct, not optimal.
- **Demand forecasting.** Needs rolling-origin validation against a
  seasonal-naive baseline; on synthetic data it would prove nothing.
- **A disruption simulator and an evaluation suite** with baseline comparisons.
- **Two of the four designed failure branches.** The clarification loop and the
  confirmation timeout are designed and documented in
  `app/workflows/routers.py`; safety review and reservation conflict are built.
- **Hosted deployment.** `docker compose` locally only.
- **Environmental impact coefficients.** Converting food weight to CO2e needs a
  methodology this project does not have, and inventing one would make the
  number worse than useless.

---

## Layout

```
backend/
  app/
    core/          settings, db, security, logging, errors, deps
    models/        13 SQLAlchemy models + every enum
    schemas/       Pydantic contracts, incl. the LLM extraction contract
    repositories/  all SQL; the only FOR UPDATE caller
    services/      donation, matching, handover, impact, safety/
    providers/     llm, routing, notify — interface + mock + real slot
    agents/        tool permission registry
    workflows/     state, routers (pure), nodes, graph
    api/v1/        auth, donations, matching, handover, workflow, impact, admin
    tasks/         the sweeper: timers, outbox, hold expiry
  migrations/      Alembic; constraints and the safety trigger live here
  tests/           unit/ (no DB) and integration/ (live PostGIS)
frontend/src/      React + TypeScript: login, donor, recipient, workflow, impact, admin
docs/              interview-guide.md, architecture.md, SPECS.md
```

---

## Troubleshooting

**`uv sync` cannot resolve a package** — relax that floor in `pyproject.toml`.
Do not change the Python version. If it is `langgraph`, it is in the optional
`agents` extra and days 1–3 do not need it.

**`type "geography" does not exist`** — PostGIS was not created. Run the
`CREATE EXTENSION` from step 1.

**`relation "donations" does not exist` in tests** — the test database has no
schema. Run `alembic upgrade head` against `DATABASE_URL_SYNC` pointed at
`foodloop_test`.

**`MissingGreenlet`** — Alembic was pointed at `DATABASE_URL` (async) instead
of `DATABASE_URL_SYNC`.

**`NotImplementedError` on any database call, Windows only** — psycopg3's async
driver needs `WindowsSelectorEventLoopPolicy`. It is set at import time in
`app/core/db.py` and `tests/conftest.py`; if you add a new entry point, set it
there too.

**`type "org_kind" already exists`** — a partial migration left enum types
behind. `alembic downgrade base` drops them; if that fails, drop and recreate
the database.

**Distances are wildly wrong** — longitude and latitude are swapped.
`ST_MakePoint` takes **lon first**. `GeoPoint.to_wkt()` gets this right; use it.

**`LangGraphUnavailable`** — install the extra, or call
`POST /api/v1/workflow/{id}/run?engine=sequential` for the checkpoint-free
path.

---

## Documentation

- [`docs/interview-guide.md`](docs/interview-guide.md) — the questions this
  project invites, and how to answer them honestly
- [`docs/architecture.md`](docs/architecture.md) — layers, data flow, decisions
- [`docs/SPECS.md`](docs/SPECS.md) — the original specifications

---

Built by **Harshini G S** (R23EA047), REVA University.
