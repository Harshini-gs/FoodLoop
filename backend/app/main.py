"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.donations import router as donations_router
from app.api.v1.handover import router as handover_router
from app.api.v1.impact import admin_router, router as impact_router
from app.api.v1.matching import router as matching_router
from app.api.v1.workflow import router as workflow_router
from app.core.db import dispose_engine, get_sessionmaker
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.settings import get_settings
from app.tasks import sweeper

log = get_logger(__name__)

DESCRIPTION = """
Agentic AI platform for coordinating surplus food donations.

**What the language model does here:** exactly two things — reads a donor's
free text into structured fields (`POST /api/v1/donations/extract`), and
writes prose over numbers computed elsewhere (`GET /api/v1/impact/narrative`).

**What it cannot do:** clear a safety check, move quantity, assign a
volunteer, or close a donation. Not because it is instructed not to, but
because it is granted no tool that could, and because the database refuses:
a `BEFORE INSERT` trigger on `reservations` rejects any donation that has not
passed the safety gate, and a `CHECK` constraint on `handover_events` refuses
a record whose confirmation code was not verified.

**Simulated data:** in the default configuration, LLM inference, route
estimates and notifications come from deterministic mock providers. Every
affected row carries a `data_source` column that is `NOT NULL` with no
default, so a simulated number cannot be stored as though it were real.

**Food safety:** the shipped ruleset is an illustrative demo with placeholder
thresholds, not verified against any authority. `GET /api/v1/donations/{id}/safety`
returns its `policy_status` so this cannot be forgotten.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()

    if settings.is_production and (
        "change-me" in settings.jwt_secret or "dev-only" in settings.jwt_secret
    ):
        raise RuntimeError("JWT_SECRET is still the development placeholder")

    # Fail fast on a malformed ruleset rather than at the first donation.
    try:
        from app.services.safety.engine import get_ruleset

        ruleset = get_ruleset()
        log.info(
            "safety ruleset loaded",
            extra={"context": {
                "version": ruleset.version,
                "rules": len(ruleset.rules),
                "policy_status": ruleset.policy_status[:60],
            }},
        )
    except Exception as exc:
        raise RuntimeError(f"safety ruleset failed to load: {exc}") from exc

    log.info(
        "foodloop starting",
        extra={"context": {
            "env": settings.app_env,
            "llm_provider": settings.llm_provider,
            "routing_provider": settings.routing_provider,
            "notify_provider": settings.notify_provider,
        }},
    )

    if settings.scheduler_enabled:
        sweeper.start(app, get_sessionmaker())

    yield

    await sweeper.stop(app)
    log.info("foodloop shutting down")
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="FoodLoop API",
        version="0.2.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        # Explicit list from settings. Never "*", because credentials are sent.
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["Idempotency-Key"],
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(donations_router, prefix="/api/v1")
    app.include_router(matching_router, prefix="/api/v1")
    app.include_router(handover_router, prefix="/api/v1")
    app.include_router(workflow_router, prefix="/api/v1")
    app.include_router(impact_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")

    return app


app = create_app()
