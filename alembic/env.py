"""Alembic environment for firogate-spark-support-CE.

IMPORTANT — read before running anything beyond `alembic stamp head`:
This repo's SQLite schema is NOT managed by Alembic. It's built by
app/core/database.py's create_tables() (Base.metadata.create_all plus a
chain of hand-written `_ensure_*` ALTER-column migration functions), run
unconditionally on every app boot — that mechanism is untouched and remains
the real source of truth for CE's schema. Alembic is wired up here only so
*future* CE schema changes can be written as real migrations instead of new
ad hoc ALTER functions, without disturbing any existing SQLite database in
the field. See alembic/versions/*_baseline_stamp.py.

`alembic upgrade head` against a fresh, empty database will create ZERO
tables — this is intentional (the baseline migration is a deliberate no-op).
Use `alembic stamp head` against an existing, already-built CE database to
mark it current, never `alembic upgrade head` expecting it to build schema.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.core.config import get_settings
from app.core.database import Base

import app.models.models  # noqa: F401
import app.models.spark    # noqa: F401

target_metadata = Base.metadata


def _sync_migration_url() -> str:
    url = get_settings().DATABASE_URL
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


config.set_main_option("sqlalchemy.url", _sync_migration_url())


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
