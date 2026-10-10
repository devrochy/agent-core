"""`platform_readiness_runs` es tabla de sistema sin RLS (HU-CONN-07): guarda el resultado de
los chequeos de la app de Meta de la plataforma y la infraestructura, que solo ve el master.
La garantía es que solo la toque `core/admin/production_readiness_service.py` (que además
exige el rol master) y que solo las rutas de plataforma pidan o lean esas corridas."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "core" / "admin" / "production_readiness_service.py"
ALLOWED_DIRS = (ROOT / "alembic" / "versions", ROOT / "tests", ROOT / "docs", ROOT / "packages")

_MODELO = re.compile(r"\bPlatformReadinessRun\b|\bplatform_readiness_runs\b")
_FUNCIONES = re.compile(r"\b(latest_platform_run|request_platform_run)\b")


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        if any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        yield path


def test_la_tabla_solo_se_usa_desde_su_servicio():
    permitidos = {SERVICE, ROOT / "core" / "db" / "models.py"}
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in permitidos and _MODELO.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"platform_readiness_runs usado fuera de su servicio: {offenders}"


def test_solo_las_rutas_de_plataforma_piden_o_leen_las_corridas():
    permitidos = {SERVICE, ROOT / "core" / "api" / "routers" / "platform.py"}
    # Los requisitos de Embedded Signup (solo master) LEEN la última corrida para no dar "lista"
    # con el checklist de la app de Meta en rojo (HU-SH-05 sobre HU-CONN-07); no piden corridas.
    solo_lectura = ROOT / "core" / "admin" / "platform_meta_es.py"
    offenders = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if p not in permitidos
        and (
            re.search(r"\brequest_platform_run\b", p.read_text(encoding="utf-8", errors="ignore"))
            if p == solo_lectura
            else _FUNCIONES.search(p.read_text(encoding="utf-8", errors="ignore"))
        )
    ]
    assert offenders == [], f"corridas de plataforma pedidas fuera de las rutas master: {offenders}"


@pytest.mark.asyncio
async def test_el_servicio_rechaza_a_quien_no_es_master():
    from types import SimpleNamespace

    from core.admin.production_readiness_service import (
        NotMaster,
        latest_platform_run,
        request_platform_run,
    )

    duenio = SimpleNamespace(id=None, role="colaborador")
    with pytest.raises(NotMaster):
        await latest_platform_run(duenio)
    with pytest.raises(NotMaster):
        await request_platform_run(duenio)
