"""Alembic environment.

Uses DATABASE_URL_SYNC, not DATABASE_URL: Alembic runs synchronously, and
pointing it at an async driver URL produces a confusing MissingGreenlet error
rather than a clear one.

`include_object` filters out PostGIS's own internal tables so autogenerate does
not try to drop them.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.settings import get_settings
from app.models import Base  # noqa: F401  — registers every table on the metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# PostGIS creates these in the public schema. They are not ours to manage.
POSTGIS_TABLES = {"spatial_ref_sys", "geography_columns", "geometry_columns", "raster_columns",
                  "raster_overviews", "topology", "layer"}


def _url() -> str:
    return get_settings().database_url_sync


def include_object(obj: object, name: str | None, type_: str, reflected: bool,
                   compare_to: object) -> bool:
    if type_ == "table" and name in POSTGIS_TABLES:
        return False
    # geoalchemy2 manages its own spatial indexes; excluding them keeps
    # autogenerate from producing a drop/create cycle on every run.
    if type_ == "index" and name and name.startswith("idx_") and "geom" in name:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
