"""Test fixtures.

Integration tests need a live PostGIS database. They are marked
`@pytest.mark.integration` and skipped with a clear message when one is not
reachable, so `pytest` never fails for the wrong reason.

Run against a throwaway database so a test can truncate freely:

    createdb -h localhost -U foodloop foodloop_test
    TEST_DATABASE_URL=postgresql+psycopg://foodloop:foodloop@localhost:5432/foodloop_test \\
      pytest -m integration
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator

# psycopg3 async needs loop.add_reader(); Windows' default ProactorEventLoop
# does not implement it. Without this every connection fails on Windows only.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://foodloop:foodloop@localhost:5432/foodloop_test",
)

# Order matters: children before parents, so TRUNCATE never trips a foreign key.
TABLES_IN_DELETE_ORDER = (
    "scheduled_tasks",
    "notifications",
    "workflow_events",
    "workflow_runs",
    "handover_events",
    "pickups",
    "reservations",
    "matches",
    "safety_checks",
    "donations",
    "safety_rule_versions",
    "users",
    "organizations",
)


async def _database_reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception as exc:  # noqa: BLE001
        # Print the real reason. Swallowing it turns any connection problem
        # into a misleading "no database" skip.
        print(f"\n[conftest] cannot reach {url}:\n  {type(exc).__name__}: {exc}\n")
        return False


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    if not await _database_reachable(TEST_DATABASE_URL):
        pytest.skip(
            f"No database at {TEST_DATABASE_URL}. "
            "Start it with `docker compose up -d db`, create the test database, "
            "and run `alembic upgrade head` against it."
        )

    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)

    # Refuse to run if migrations have not been applied — otherwise every test
    # fails with a confusing "relation does not exist".
    async with eng.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'donations'"
            )
        )
        if result.scalar_one() == 0:
            await eng.dispose()
            pytest.skip("Schema missing. Run `alembic upgrade head` against the test database.")

    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session with the tables emptied first. Not wrapped in an outer
    rollback: the concurrency tests need real committed transactions."""
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(TABLES_IN_DELETE_ORDER)} CASCADE")
        )

    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def raw_connection(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A SECOND independent session, for tests that need two real connections
    racing each other. Sharing one session would serialize them in the client
    and the test would prove nothing."""
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs a live PostGIS database")
