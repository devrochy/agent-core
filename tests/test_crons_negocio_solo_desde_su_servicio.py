"""`tenant_cron_configs` es una tabla de NEGOCIO con RLS (ADR-0201).

La garantía es que solo la toque `core/admin/crons_negocio_service.py`; el helper de los crons
(`core/tasks/cron_negocio.py`) y la ruta de Automatizaciones pasan por ese servicio.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "core" / "admin" / "crons_negocio_service.py"
MODELOS = ROOT / "core" / "db" / "models.py"
ALLOWED_DIRS = (ROOT / "alembic" / "versions", ROOT / "tests", ROOT / "docs")

_TABLA = re.compile(
    r"\bTenantCron(?:Config|Run)\b|\b(?:from|into|update|join)\s+tenant_cron_(?:configs|runs)\b",
    re.IGNORECASE,
)
_USUARIOS_DEL_SERVICIO = {
    ROOT / "core" / "tasks" / "cron_negocio.py",
    ROOT / "core" / "api" / "routers" / "automatizaciones.py",
    # Vista master por negocio y purga del historial desde el mantenimiento de la plataforma.
    ROOT / "core" / "api" / "routers" / "crons.py",
    ROOT / "core" / "admin" / "crons_service.py",
}


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        if any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        yield path


def test_la_tabla_solo_se_usa_desde_su_servicio():
    permitidos = {SERVICE, MODELOS}
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in permitidos and _TABLA.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"tenant_cron_configs usada fuera de su servicio: {offenders}"


def test_el_servicio_solo_lo_importan_el_helper_y_la_ruta():
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in _USUARIOS_DEL_SERVICIO
        and p not in {SERVICE, MODELOS}
        and re.search(r"crons_negocio_service", p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"crons_negocio_service usado desde un lugar no previsto: {offenders}"


def test_las_migraciones_declaran_rls_y_tenant_id():
    for nombre in ("0142_crons_por_negocio.py", "0143_historial_crons_negocio.py"):
        migracion = (ROOT / "alembic" / "versions" / nombre).read_text(encoding="utf-8")
        assert "tenant_id" in migracion, nombre
        assert "ENABLE ROW LEVEL SECURITY" in migracion, nombre
        assert "FORCE ROW LEVEL SECURITY" in migracion, nombre
