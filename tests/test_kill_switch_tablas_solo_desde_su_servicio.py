"""`platform_kill_switch` y `platform_kill_switch_events` son tablas de sistema sin RLS (HU-ARCH-38, ADR-0200):
el interruptor global de la plataforma y su historial, sin datos de ningún negocio. La garantía es que solo las
escriba `core/admin/kill_switch_service.py` y que el camino caliente las lea solo por `core/shared/kill_switch.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "core" / "admin" / "kill_switch_service.py"
LECTOR = ROOT / "core" / "shared" / "kill_switch.py"
MODELOS = ROOT / "core" / "db" / "models.py"
ALLOWED_DIRS = (ROOT / "alembic" / "versions", ROOT / "tests", ROOT / "docs")

_TABLAS = re.compile(
    r"\bPlatformKillSwitch(?:Event)?\b|\b(?:from|into|update|join)\s+platform_kill_switch(?:_events)?\b",
    re.IGNORECASE,
)
_USUARIOS_DEL_SERVICIO = {
    ROOT / "core" / "api" / "routers" / "kill_switch.py",
    ROOT / "cli" / "commands" / "admin.py",
    ROOT / "core" / "orchestrator" / "debounce.py",  # `ultima_reactivacion`, solo lectura
}


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        if any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        yield path


def test_las_tablas_solo_se_usan_desde_su_servicio_y_su_lector():
    permitidos = {SERVICE, LECTOR, MODELOS}
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in permitidos and _TABLAS.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"platform_kill_switch usada fuera de su servicio: {offenders}"


def test_el_servicio_solo_lo_usan_la_ruta_la_cli_y_la_recuperacion():
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in _USUARIOS_DEL_SERVICIO
        and p not in {SERVICE, MODELOS, LECTOR}
        and re.search(r"kill_switch_service", p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"kill_switch_service usado desde un lugar no previsto: {offenders}"


def test_la_migracion_declara_las_tablas_de_sistema():
    migracion = (ROOT / "alembic" / "versions" / "0140_interruptor_global.py").read_text(encoding="utf-8")
    assert migracion.lstrip('"').startswith("Tabla de SISTEMA (sin RLS")
    assert "INSERT INTO platform_kill_switch (id, active) VALUES (1, false)" in migracion
    sin_menciones = migracion.replace("sin `tenant_id`", "").replace("`tenant_scope`", "")
    assert "tenant_id" not in sin_menciones
