"""Health endpoint.

Every field is a real check. A health endpoint that returns a hard-coded "ok"
tells you nothing when the thing you need to know is whether the database is
reachable and the schema is current.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.schemas.common import HealthResponse

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/health", response_model=HealthResponse)
async def health(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    settings = get_settings()

    db_status = "error"
    postgis_version: str | None = None
    migration_rev: str | None = None

    try:
        await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:  # noqa: BLE001 — health must report, not raise
        log.warning("health: database unreachable", extra={"context": {"error": str(exc)}})
        return HealthResponse(
            status="degraded",
            db="error",
            app_env=settings.app_env,
            checked_at=datetime.now(UTC),
        )

    try:
        result = await session.execute(text("SELECT postgis_version()"))
        postgis_version = result.scalar_one()
    except Exception as exc:  # noqa: BLE001
        log.warning("health: postgis missing", extra={"context": {"error": str(exc)}})

    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        migration_rev = result.scalar_one_or_none()
    except Exception:  # noqa: BLE001 — table absent before the first migration
        migration_rev = None

    # "ok" requires all three. PostGIS absent means matching cannot work, so
    # reporting "ok" would be misleading.
    overall = "ok" if (postgis_version and migration_rev) else "degraded"

    return HealthResponse(
        status=overall,
        db=db_status,
        postgis=postgis_version,
        migration=migration_rev,
        app_env=settings.app_env,
        checked_at=datetime.now(UTC),
    )


@router.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    """Process-is-up only. No dependencies, so a database outage does not cause
    an orchestrator to restart a healthy process."""
    return {"status": "alive"}
