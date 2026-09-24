"""Camino único para aplicar migraciones: core + plugins.

Existía una implementación distinta en cada entrada (`agent-core init`,
`agent-core db migrate`, `make migrate`, el entrypoint del contenedor) y
divergían en qué migraban. Todo pasa ahora por aquí.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config as AlembicConfig

from alembic import command
from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)


def project_root() -> Path:
    """Raíz del proyecto: el directorio que contiene `alembic.ini`.

    Se busca primero subiendo desde este módulo y luego desde el CWD, para que
    las migraciones funcionen igual desde el host, desde el contenedor o
    invocadas desde cualquier subdirectorio.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "alembic.ini").exists():
            return candidate
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "alembic.ini").exists():
            return candidate
    return cwd


def sync_database_url() -> str:
    """URL síncrona (psycopg2) para Alembic, escapando `%` para ConfigParser.

    Las migraciones corren como el DUEÑO (MIGRATIONS_DATABASE_URL); el runtime
    usa app_runtime y no tiene permiso de DDL. Vacío -> DATABASE_URL.

    Se fija `+psycopg2` explícito: en SQLAlchemy 2.1 el dialecto por defecto de
    `postgresql://` pasó a psycopg (v3), que no es dependencia del repo.
    """
    url = (get_settings().migrations_database_url or get_settings().database_url).replace("+asyncpg", "+psycopg2")
    return url.replace("%", "%%")


def _core_config() -> AlembicConfig:
    root = project_root()
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_database_url())
    return cfg


def upgrade_core() -> None:
    """Aplica las migraciones del core hasta head."""
    command.upgrade(_core_config(), "head")
    log.info("core_migrations_applied")


def plugin_dir(plugin_id: str) -> Path:
    """Directorio del pack, resuelto por entry_point id (packages/pack-* o plugins/*).

    El id del entry_point ("pack_commerce") no coincide con el nombre del directorio
    ("pack-commerce"), así que se lee el pyproject para encontrar el que lo declara.
    """
    import tomllib

    root = project_root()
    for base in (root / "packages", root / "plugins"):
        if not base.exists():
            continue
        for candidate in sorted(base.iterdir()):
            pyproject = candidate / "pyproject.toml"
            if not pyproject.exists():
                continue
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                eps = (
                    data.get("project", {})
                    .get("entry-points", {})
                    .get("agent_core.plugins", {})
                )
            except Exception:
                continue
            if plugin_id in eps:
                return candidate
    return root / "plugins" / plugin_id


def upgrade_plugin(plugin_id: str) -> bool:
    """Aplica el árbol Alembic propio del plugin. False si no tiene ninguno.

    Cada plugin usa su propia version_table (`alembic_version_<plugin_id>`)
    para no chocar con el árbol del core ni con el de otros plugins.
    """
    directory = plugin_dir(plugin_id)
    ini_path = directory / "alembic.ini"
    if not ini_path.exists():
        # Es legítimo (un plugin sin tablas propias), pero se registra: antes
        # este caso era un `return` mudo indistinguible de una ruta mal resuelta.
        log.info("plugin_sin_migraciones", plugin=plugin_id, esperado=str(ini_path))
        return False

    cfg = AlembicConfig(str(ini_path))
    cfg.set_main_option("script_location", str(directory / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_database_url())
    cfg.set_main_option("version_table", f"alembic_version_{plugin_id}")
    command.upgrade(cfg, "head")
    log.info("plugin_migrations_applied", plugin=plugin_id)
    return True


def upgrade_all(plugin_ids: list[str] | None = None) -> dict[str, str]:
    """Core + todos los plugins detectados. Devuelve {plugin_id: estado}.

    Un fallo en un plugin no aborta el resto: se reporta y se continúa, igual
    que hace el aislamiento de plugins en runtime.
    """
    upgrade_core()
    results: dict[str, str] = {}

    if plugin_ids is None:
        from core.plugin_manager import plugin_manager

        plugin_manager.scan()
        plugin_ids = [module.id for module in plugin_manager.list_modules()]

    for plugin_id in plugin_ids:
        try:
            applied = upgrade_plugin(plugin_id)
            results[plugin_id] = "aplicadas" if applied else "sin migraciones"
        except Exception as exc:
            results[plugin_id] = f"error: {exc}"
            log.error("plugin_migrations_failed", plugin=plugin_id, error=str(exc))
    return results
