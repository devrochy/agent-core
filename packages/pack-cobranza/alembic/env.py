"""Árbol Alembic PROPIO del plugin pack_cobranza.

version_table separada (alembic_version_pack_cobranza) para no chocar con el
core ni con otros packs. El plugin_manager ejecuta esto al activar el pack.
"""

from __future__ import annotations

from pack_cobranza.models import Base
from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if not config.get_main_option("sqlalchemy.url"):
    from core.config import get_settings

    # Las migraciones corren como el DUEÑO (MIGRATIONS_DATABASE_URL); el runtime
    # usa app_runtime y no tiene permiso para DDL. Vacío -> DATABASE_URL.
    _url = get_settings().migrations_database_url or get_settings().database_url
    config.set_main_option("sqlalchemy.url", _url.replace("+asyncpg", ""))

target_metadata = Base.metadata
VERSION_TABLE = config.get_main_option("version_table") or "alembic_version_pack_cobranza"


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
