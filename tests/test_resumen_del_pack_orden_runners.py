"""El resumen de confirmación de un pack sobre una tool del core (`sub_confirmar_inscripcion`) depende del orden de arranque.

Corrida #17 de pass^4 (2026-11-04): los runners de evals activaban los packs ANTES de registrar las tools del core, así
que `register_resumen` fallaba (la tool aún no existía) y los journeys confirmaban con el texto de respaldo, con el
`monto_a_pagar` que firmó el modelo: tras cambiar de plan salía «PREMIUM-ANO … 600.000». La app real
(`core/main.py`) registra las tools del core al importar, antes de activar los packs. Sin BD ni red.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.mcp.host import MCPHost
from core.plugin_manager import PluginError
from core.tools.core_skill import register_core_skills

RAIZ = Path(__file__).resolve().parents[1]


async def _resumen(accion: str, args: dict, ctx):  # type: ignore[no-untyped-def]
    return None


def test_con_las_tools_del_core_primero_el_pack_registra_el_resumen() -> None:
    host = MCPHost()
    register_core_skills(host)
    host.begin_activation("pack_subscriptions")
    try:
        host.register_resumen("sub_confirmar_inscripcion", _resumen)
    finally:
        host.end_activation()
    assert host.get_resumen("sub_confirmar_inscripcion") is not None


def test_si_el_pack_se_activa_antes_el_resumen_no_se_registra() -> None:
    """Es lo que pasaba en los runners: `register_resumen` falla y el pack lo deja como aviso."""
    host = MCPHost()
    host.begin_activation("pack_subscriptions")
    try:
        with pytest.raises(PluginError):
            host.register_resumen("sub_confirmar_inscripcion", _resumen)
    finally:
        host.end_activation()
    assert host.get_resumen("sub_confirmar_inscripcion") is None


@pytest.mark.parametrize("archivo", ["evals/journeys.py", "evals/runner.py"])
def test_los_runners_registran_las_tools_del_core_antes_de_activar_los_packs(archivo: str) -> None:
    fuente = (RAIZ / archivo).read_text(encoding="utf-8")
    registrar = [m.start() for m in re.finditer(r"^\s+register_core_skills\(", fuente, flags=re.MULTILINE)]
    activar = [m.start() for m in re.finditer(r"await plugin_manager\.activate_all_configured\(\)", fuente)]
    assert registrar and activar, f"{archivo}: no encuentro register_core_skills o activate_all_configured"
    assert max(registrar) < min(activar), f"{archivo}: las tools del core deben registrarse antes de activar los packs"
