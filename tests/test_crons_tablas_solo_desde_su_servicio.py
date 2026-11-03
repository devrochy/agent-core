"""`cron_job_configs` y `cron_runs` son tablas de sistema sin RLS (ADR-0183): guardan el estado
operativo y el historial de los crons de la plataforma, sin datos de ningún negocio. La garantía
es que solo las toque `core/admin/crons_service.py` (que a su vez solo se invoca desde rutas
master-only, el wrapper de los crons y el planificador del beat).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "core" / "admin" / "crons_service.py"
MODELOS = ROOT / "core" / "db" / "models.py"
ALLOWED_DIRS = (ROOT / "alembic" / "versions", ROOT / "tests", ROOT / "docs")

# Las clases del modelo y el SQL a mano (no las menciones en docstrings o en el nombre de un cron).
_TABLAS = re.compile(
    r"\bCronRun\b|\bCronJobConfig\b|\b(?:from|into|update|join)\s+(?:cron_runs|cron_job_configs)\b",
    re.IGNORECASE,
)
# Quién puede importar el servicio: rutas, el wrapper y el planificador (y el propio catálogo/CLI no).
_USUARIOS_DEL_SERVICIO = {
    ROOT / "core" / "api" / "routers" / "crons.py",
    ROOT / "core" / "tasks" / "cron_runtime.py",
    ROOT / "core" / "tasks" / "cron_scheduler.py",
    ROOT / "core" / "tasks" / "celery_app.py",  # el cron de mantenimiento llama a `mantenimiento`
}


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        if any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        yield path


def test_las_tablas_solo_se_usan_desde_su_servicio():
    permitidos = {SERVICE, MODELOS}
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in permitidos and _TABLAS.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"cron_runs/cron_job_configs usadas fuera de su servicio: {offenders}"


def test_el_servicio_solo_lo_importan_las_rutas_el_wrapper_y_el_planificador():
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in _USUARIOS_DEL_SERVICIO
        and p not in {SERVICE, MODELOS}  # el modelo lo nombra en su docstring
        and re.search(r"crons_service", p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"crons_service usado desde un lugar no previsto: {offenders}"


def test_la_migracion_declara_las_tablas_de_sistema():
    migracion = (ROOT / "alembic" / "versions" / "0120_gestion_de_crons.py").read_text(encoding="utf-8")
    assert "tabla de sistema" in migracion.lower()
    assert "tenant_id" not in migracion.replace("sin `tenant_id`", "")  # sin columna de negocio
