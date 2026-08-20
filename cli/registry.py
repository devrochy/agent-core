"""Registry de plugins.

Resolución de `agent-core plugin install <nombre>`:
1. Carpeta local ./plugins/<nombre>/ (desarrollo local, prioridad).
2. GitHub: https://github.com/agent-core-plugins/<nombre> (git clone).

El org de GitHub es configurable con AGENT_CORE_PLUGIN_ORG.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PLUGINS_DIR = Path("plugins")
DEFAULT_ORG = os.environ.get("AGENT_CORE_PLUGIN_ORG", "agent-core-plugins")

# Plugins conocidos (para `agent-core plugin list`)
KNOWN_PLUGINS: dict[str, str] = {
    "mod-crm-engine": "CRM: leads, catálogo (menú), promociones y pedidos",
    "mod-google-calendar": "Google Calendar: agendamiento con OAuth2",
    "mod-media-processor": "Audio (STT) e imágenes (vision) entrantes",
}


def normalize(name: str) -> str:
    """mod-crm-engine -> mod_crm_engine (nombre de paquete Python)."""
    return name.replace("-", "_")


def resolve_local(name: str) -> Path | None:
    for candidate in (PLUGINS_DIR / normalize(name), PLUGINS_DIR / name):
        if candidate.is_dir() and (candidate / "pyproject.toml").exists():
            return candidate
    return None


def clone_from_github(name: str) -> Path:
    url = f"https://github.com/{DEFAULT_ORG}/{name}.git"
    target = PLUGINS_DIR / normalize(name)
    PLUGINS_DIR.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", url, str(target)], check=True)
    return target


def pip_install_editable(path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(path.resolve())],
        check=True, capture_output=True, text=True,
    )
    # Después de instalar, asegurar que el módulo sea importable en el proceso actual.
    # pip instala el paquete con un editable finder (.pth) que solo se activa al
    # arrancar Python. Como corremos en el mismo proceso, procesamos site-packages
    # manualmente para que el módulo quede disponible de inmediato.
    _refresh_site_packages(path)


def _refresh_site_packages(path: Path) -> None:
    """Procesa los site-packages para activar el finder del editable install."""
    import importlib
    import site

    # Invalida caché de importación (detecta nuevos módulos instalados)
    importlib.invalidate_caches()

    # Procesa site-packages del usuario (donde pip install --user deposita los .pth)
    try:
        user_site = site.getusersitepackages()
        site.addsitedir(user_site)
    except Exception:
        pass

    # Procesa site-packages del sistema
    for site_dir in site.getsitepackages():
        try:
            site.addsitedir(site_dir)
        except Exception:
            pass

    # Fallback directo: añadir el directorio fuente del plugin a sys.path
    src = str(path.resolve())
    if src not in sys.path:
        sys.path.insert(0, src)
