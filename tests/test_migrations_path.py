"""Resolución de rutas de migraciones.

Regresión: `_run_plugin_migrations` construía `Path("plugins") / plugin_id`,
relativo al CWD, y retornaba en silencio si no existía. Invocado desde otro
directorio no migraba nada y no lo decía.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.db.migrations import plugin_dir, project_root, sync_database_url

ROOT = Path(__file__).resolve().parent.parent


def test_project_root_encuentra_alembic_ini() -> None:
    assert (project_root() / "alembic.ini").exists()


def test_project_root_no_depende_del_cwd(tmp_path: Path) -> None:
    anterior = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert project_root() == ROOT
    finally:
        os.chdir(anterior)


def test_plugin_dir_es_absoluto_y_no_depende_del_cwd(tmp_path: Path) -> None:
    anterior = Path.cwd()
    try:
        os.chdir(tmp_path)
        destino = plugin_dir("pack_commerce")
        assert destino.is_absolute()
        assert destino == ROOT / "packages" / "pack-commerce"
    finally:
        os.chdir(anterior)


def test_plugin_dir_apunta_a_un_plugin_real() -> None:
    assert (plugin_dir("pack_commerce") / "alembic.ini").exists()


def test_sync_database_url_quita_el_driver_async() -> None:
    """Fija psycopg2 explícito: en SQLAlchemy 2.1 el dialecto por defecto de
    `postgresql://` pasó a psycopg (v3), que no es dependencia del repo."""
    url = sync_database_url()
    assert "+asyncpg" not in url
    assert url.startswith("postgresql+psycopg2://")


def test_sync_database_url_escapa_porcentajes() -> None:
    """ConfigParser interpreta `%` como interpolación: una contraseña con `%`
    rompía `set_main_option` al configurar Alembic."""
    from core.config import get_settings

    get_settings.cache_clear()
    anterior = os.environ.get("DATABASE_URL")
    anterior_mig = os.environ.get("MIGRATIONS_DATABASE_URL")
    # sin MIGRATIONS_DATABASE_URL (vacío en env, sobre .env), cae a DATABASE_URL
    os.environ["MIGRATIONS_DATABASE_URL"] = ""
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://u:pa%ss@localhost:5432/db"
    try:
        assert "pa%%ss" in sync_database_url()
    finally:
        if anterior is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = anterior
        if anterior_mig is None:
            del os.environ["MIGRATIONS_DATABASE_URL"]
        else:
            os.environ["MIGRATIONS_DATABASE_URL"] = anterior_mig
        get_settings.cache_clear()
