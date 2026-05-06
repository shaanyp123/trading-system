"""Alembic environment.

Reads `DATABASE_URL` from the process environment (loaded by the runtime from
sops per backend-spec §8.1.3). No plaintext URL ever lives in the repo.

Migrations are hand-written (no autogenerate); `target_metadata` is therefore
intentionally `None`. Per dev-guide §7.1: "Both upgrade() AND downgrade()
always implemented and tested."
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _resolve_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic requires a connection string from the "
            "runtime environment (sops-decrypted at deploy time per backend-spec §8.1.3). "
            "For local migration authoring use a throwaway Postgres URL."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade --sql ...`)."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = _resolve_database_url()
    connectable = engine_from_config(
        config_section,
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
