"""Database engine and session management.

Two things worth noting:

1. Isolation level is READ COMMITTED (Postgres default). The reservation path
   relies on `SELECT ... FOR UPDATE` row locks rather than SERIALIZABLE, so
   there is no serialization-failure retry loop on every write. See
   docs/SPECS.md section 1.

2. `expire_on_commit=False` so objects stay usable after commit. Without it,
   every attribute access after a commit triggers a lazy reload, which in async
   SQLAlchemy raises MissingGreenlet.
"""

import asyncio
import sys
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import get_settings

# psycopg3's async driver uses loop.add_reader(), which Windows' default
# ProactorEventLoop does not implement — every async connection would fail
# with NotImplementedError. Set at import time, before any loop is created.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request, rolled back on exception."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Called on application shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
