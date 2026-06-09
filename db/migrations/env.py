"""Alembic migration environment. Edit DATABASE_URL_SYNC in alembic.ini."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import Base metadata from ORM models — connected in M1
import apps.api.models  # noqa: E402, F401 — registers all ORM models on Base.metadata
from apps.api.core.database import Base  # noqa: E402

target_metadata = Base.metadata

# Override sqlalchemy.url from environment variable
database_url = os.environ.get("DATABASE_URL_SYNC", "")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# ---------------------------------------------------------------------------
# Known false-positive constraints
# ---------------------------------------------------------------------------
# M1 constraints created inline inside op.create_table() are not tracked by
# Alembic autogenerate in subsequent runs (documented in migration 002 notes).
# The constraints DO exist in the database — autogenerate just cannot see them.
# Excluding them here prevents alembic check from reporting false drift.
_KNOWN_INLINE_CONSTRAINTS: frozenset[str] = frozenset({
    # M1 — created inline inside op.create_table() in migration 001.
    "uq_refresh_tokens_jti",
    "uq_refresh_tokens_token_hash",
    "uq_tenants_slug",
    "uq_users_email",
    # M3.1 — created inline inside op.create_table() in migration 004.
    "uq_source_configs_code",
    # M3.3 — created inline inside op.create_table() in migration 005.
    "uq_filings_accession_number",
    # M3.6 — created inline inside op.create_table() in migration 006.
    "uq_stored_documents_accession_number",
})


def _include_object(object: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:  # type: ignore[override]
    """
    Exclude known inline constraints from autogenerate comparison.

    Called by Alembic for every schema object during autogenerate.
    Returns False to exclude an object from the comparison.
    """
    if type_ == "unique_constraint" and name in _KNOWN_INLINE_CONSTRAINTS:
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_comments=False,          # suppress column-comment drift
        include_object=_include_object,  # suppress known M1 inline constraints
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_comments=False,          # suppress column-comment drift
            include_object=_include_object,  # suppress known M1 inline constraints
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
