"""Alembic environment.

Migrations run against `DATABASE_URL_DIRECT` when it is set. Neon's pooled endpoint is
PgBouncer in transaction mode, which is fine for the API's short parameterised queries and a
poor fit for DDL: `CREATE EXTENSION`, advisory locks and multi-statement transactions all
behave better on a direct connection. Falling back to the pooled URL keeps a single-URL setup
working, it is just not the one to use for a large migration.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from haazir.config import settings
from haazir.db import Base, _requires_ssl
from haazir.models import *  # noqa: F401,F403  — populates Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_URL = settings.sqlalchemy_url_direct or settings.sqlalchemy_url
if not _URL:
    raise SystemExit(
        "DATABASE_URL is not set.\n"
        "  cp api/.env.example api/.env  and paste your Neon connection string."
    )
config.set_main_option("sqlalchemy.url", _URL)


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep PostGIS's own bookkeeping out of autogenerate.

    The postgis extension creates `spatial_ref_sys` and a handful of views in the public
    schema. Autogenerate sees them as tables the models do not define and cheerfully writes a
    migration that drops them, which breaks every geography column in the database.
    """
    if type_ == "table" and name in {
        "spatial_ref_sys",
        "geography_columns",
        "geometry_columns",
        "raster_columns",
        "raster_overviews",
        "layer",
        "topology",
    }:
        return False
    if type_ == "index" and name and name.startswith("idx_"):
        # GeoAlchemy2's implicit spatial indexes; ours are declared explicitly in migrations.
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Decided from the URL migrations actually connect to. `settings.db_requires_ssl`
        # looked at DATABASE_URL, which is a different host from DATABASE_URL_DIRECT on Neon
        # and can be a different branch entirely during tests.
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            **({"ssl": "require"} if _requires_ssl(_URL) else {}),
        },
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
