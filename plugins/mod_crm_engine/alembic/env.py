"""Árbol Alembic PROPIO del plugin mod_crm_engine.

version_table separada (alembic_version_mod_crm_engine) para no chocar con el
core ni con otros plugins. El plugin_manager ejecuta esto al activar el plugin.
"""

from __future__ import annotations

from mod_crm_engine.models import Base
from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if not config.get_main_option("sqlalchemy.url"):
    from core.config import get_settings

    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("+asyncpg", ""))

target_metadata = Base.metadata
VERSION_TABLE = config.get_main_option("version_table") or "alembic_version_mod_crm_engine"


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table=VERSION_TABLE,
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
            connection=connection, target_metadata=target_metadata, version_table=VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
