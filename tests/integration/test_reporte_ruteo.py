"""HU-AOS-17 · el reporte del enrutamiento con el loop real y un modelo guionado (sin LLM).

- **Sombra:** una predicción que el agente por defecto no inicia queda **sin verdad** (no es una
  precisión de 0); la que inicia, acierta. El origen es `sombra`.
- **Activo:** no se escribe `shadow_route`, y aun así el reporte mide el disparador (primer candidato)
  contra lo que inicia el especialista; el origen es `activo`.
- **Golden:** la verdad por escenario contra la release activa de la conexión, sin tráfico ni inicios.
- **Aislamiento:** otro negocio no ve esos turnos, y pedir el reporte fuera del `tenant_scope` falla.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from test_enrutamiento_especialistas import (
    CITA,
    DEMO,
    PLAN,
    _con_dos_especialistas,
    _conversacion,
    _decision,
    _modo,
    _negocio,
    _proceso,  # noqa: F401  (fixture de módulo, autouse: registra los packs en el proceso)
    _turno,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PREMIUM = "quiero el plan premium"


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _reporte(tid: uuid.UUID) -> Any:
    from core.kernel.ruteo_sombra import reporte_de_sombra
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await reporte_de_sombra(tid, 7)


def _por(reporte: Any) -> dict[str, Any]:
    return {m.procedure_id: m for m in reporte.metricas}


async def test_sombra_sin_inicio_queda_sin_verdad_y_activo_mide_el_disparador(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas("shadow")

    # Sombra: el router predice el plan y Ventas (por defecto) lo inicia → acierto.
    c1 = await _conversacion(tid, cid)
    await _turno(tid, c1, monkeypatch, [("iniciar_procedimiento", {"procedure_id": PLAN}), "¿Qué plan?"], texto=PREMIUM)
    # Sombra: el router predice la cita, Ventas no la declara y no inicia nada → sin verdad.
    c2 = await _conversacion(tid, cid)
    await _turno(tid, c2, monkeypatch, ["Claro, te ayudo."], texto=DEMO)

    r = await _reporte(tid)
    por = _por(r)
    assert r.por_origen == {"sombra": 2} and r.turnos == 2
    assert (por[PLAN].predichos, por[PLAN].reales, por[PLAN].precision, por[PLAN].recall) == (1, 1, 1.0, 1.0)
    # El caso del bug: predicción correcta, nada iniciado → antes 0,00 de precisión; ahora no medible.
    assert (por[CITA].predichos, por[CITA].reales, por[CITA].sin_verdad, por[CITA].precision) == (1, 0, 1, None)

    # Activo: no hay shadow_route de ruta, el reporte sigue midiendo (por `candidates`).
    await _modo(tid, "active")
    c3 = await _conversacion(tid, cid)
    await _turno(tid, c3, monkeypatch, [("iniciar_procedimiento", {"procedure_id": CITA}), "¿Qué día?"], texto=DEMO)
    d = await _decision(tid, c3)
    assert d["route_source"] == "trigger" and d["shadow_route"] is None

    r = await _reporte(tid)
    por = _por(r)
    assert r.por_origen == {"sombra": 2, "activo": 1} and r.reglas_activas == 0
    assert (por[CITA].predichos, por[CITA].reales, por[CITA].verdaderos, por[CITA].sin_verdad) == (2, 1, 1, 1)
    assert por[CITA].precision == 1.0 and por[CITA].recall == 1.0


async def test_el_golden_se_mide_contra_la_release_activa_sin_trafico(_ruteo: None) -> None:
    from core.kernel.ruteo_sombra import CasoGolden, reporte_de_golden
    from core.shared.tenant import tenant_scope

    tid, cid, _inst = await _con_dos_especialistas("off")
    casos = [CasoGolden(DEMO, CITA, "c"), CasoGolden(PREMIUM, PLAN, "c"), CasoGolden("quiero un pedido", "pack_commerce.x", "c")]
    async with tenant_scope(tid):
        por_conexion = await reporte_de_golden(tid, casos)
    (g,) = por_conexion
    assert g.connection_id == cid and g.release_id is not None
    assert (g.reporte.turnos, g.no_aplicables) == (2, 1)  # el procedimiento que la release no tiene no se puntúa
    assert g.fallos == []
    assert {m.procedure_id: (m.precision, m.recall) for m in g.reporte.metricas} == {CITA: (1.0, 1.0), PLAN: (1.0, 1.0)}


async def test_aislamiento_del_reporte(_ruteo: None, monkeypatch: Any) -> None:
    from core.kernel.ruteo_sombra import CasoGolden, reporte_de_golden, reporte_de_sombra
    from core.shared.tenant import tenant_scope

    tid, cid, _inst = await _con_dos_especialistas("shadow")
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["Claro."], texto=DEMO)
    otro, _ = await _negocio()

    assert (await _reporte(tid)).turnos == 1
    assert (await _reporte(otro)).turnos == 0
    async with tenant_scope(otro):
        propios = await reporte_de_golden(otro, [CasoGolden(DEMO, CITA, "c")])
        assert [g.connection_id for g in propios] != [cid]  # solo sus conexiones (la release 1 de su negocio)
        with pytest.raises(PermissionError):
            await reporte_de_sombra(tid, 7)  # el negocio de otro scope
        with pytest.raises(PermissionError):
            await reporte_de_golden(tid, [])
