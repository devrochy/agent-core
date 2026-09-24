"""Entorno Alembic del CORE (los plugins tienen su propio árbol, ver PLUGIN-CONTRACT.md)."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from core.config import get_settings
from core.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL síncrona (psycopg2) para migraciones. Las migraciones corren como el DUEÑO
# de las tablas (MIGRATIONS_DATABASE_URL); el runtime conecta con app_runtime para
# que RLS se aplique (ADR-0001). Vacío → usa DATABASE_URL.
_migrations_url = get_settings().migrations_database_url or get_settings().database_url
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", _migrations_url.replace("+asyncpg", "+psycopg2"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
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
        # include_schemas=True: desde meta_private (migración 0018), autogenerate
        # y `alembic check` reflejan también schemas que no son `public` -- sin
        # esto, cualquier tabla fuera de `public` aparece como "falta crear" aunque
        # ya exista (falso positivo de la comparación, no un drift real).
        context.configure(connection=connection, target_metadata=target_metadata, include_schemas=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
