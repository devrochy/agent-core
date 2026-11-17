"""Pausa por agente con el loop real y un modelo guionado (HU-ARCH-38, ADR-0200). Sin LLM.

Negocio con Ventas (por defecto) y Agenda publicados (los helpers de `test_enrutamiento_especialistas`):

- **Especialista pausado:** el disparador iría a Agenda, pero atiende Ventas; el dueño no cambia y al reanudar
  Agenda vuelve a atender.
- **Sin a quién pasar:** con el de por defecto pausado, el turno se cierra en silencio con `kill_switch`.
- **Sin publicar:** pausar y reanudar no crea ninguna release.
- **Aislamiento:** pausar el agente de un negocio no cambia el otro.
- **No se pausa el Coordinador.**
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from test_enrutamiento_especialistas import (
    DEMO,
    _con_dos_especialistas,
    _conversacion,
    _decision,
    _dueno,
    _sql,
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


async def _agente(tid: uuid.UUID, key: str) -> uuid.UUID:
    ((agent_id,),) = await _sql(tid, "select id from agents where key = :k and deleted_at is null", k=key)
    return agent_id


async def _pausar(tid: uuid.UUID, key: str, pausar: bool = True) -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await versiones.pausar_agente(tid, await _agente(tid, key), pausar=pausar, admin_id=None, motivo="prueba")


async def _turnos(tid: uuid.UUID, conv: uuid.UUID) -> list[tuple[str, str | None]]:
    filas = await _sql(
        tid, "select outcome, blocked_by from core_decisions where conversation_id = :c order by created_at", c=conv
    )
    return [(f[0], f[1]) for f in filas]


async def _releases(tid: uuid.UUID) -> int:
    ((n,),) = await _sql(tid, "select count(*) from connection_releases")
    return int(n)


async def test_un_especialista_pausado_no_atiende_y_al_reanudar_vuelve(monkeypatch: Any) -> None:
    tid, _cid, inst = await _con_dos_especialistas()
    conv = await _conversacion(tid, _cid)
    await _turno(tid, conv, monkeypatch, ["¿Para qué día?"], texto=DEMO)
    assert await _dueno(tid, conv) == inst["agenda"], "sin pausa, el disparador pasa el turno a Agenda"

    releases_antes = await _releases(tid)
    await _pausar(tid, "agenda")
    await _turno(tid, conv, monkeypatch, ["Te ayudo yo."], texto=DEMO)
    d = await _decision(tid, conv)
    assert d["active_instance_id"] == str(inst["ventas"]), "atiende el agente por defecto"
    assert await _dueno(tid, conv) == inst["agenda"], "el dueño no cambia mientras dura la pausa"

    await _pausar(tid, "agenda", pausar=False)
    await _turno(tid, conv, monkeypatch, ["¿Para qué día?"], texto="gracias")
    assert (await _decision(tid, conv))["active_instance_id"] == str(inst["agenda"]), "al reanudar vuelve"
    assert await _releases(tid) == releases_antes, "pausar y reanudar no publica nada"


async def test_con_el_de_por_defecto_pausado_el_turno_se_cierra_en_silencio(monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas()
    conv = await _conversacion(tid, cid)
    await _pausar(tid, "ventas")
    g = await _turno(tid, conv, monkeypatch, ["No debería responder."], texto="hola")
    assert await _turnos(tid, conv) == [("blocked", "kill_switch")]
    assert g.tools == [], "no se llamó al modelo"


async def test_pausar_un_agente_no_afecta_a_otro_negocio(monkeypatch: Any) -> None:
    tid_a, _cid_a, _ = await _con_dos_especialistas()
    tid_b, cid_b, inst_b = await _con_dos_especialistas()
    await _pausar(tid_a, "agenda")
    conv_b = await _conversacion(tid_b, cid_b)
    await _turno(tid_b, conv_b, monkeypatch, ["¿Para qué día?"], texto=DEMO)
    assert (await _decision(tid_b, conv_b))["active_instance_id"] == str(inst_b["agenda"])
    ((pausados_b,),) = await _sql(tid_b, "select count(*) from agents where paused_at is not null")
    assert pausados_b == 0


async def test_el_coordinador_no_se_pausa() -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    tid, _cid, _ = await _con_dos_especialistas()
    filas = await _sql(tid, "select id from agents where kind = 'core' and deleted_at is null")
    async with tenant_scope(tid):
        with pytest.raises(versiones.Conflicto) as exc:
            await versiones.pausar_agente(tid, filas[0][0], pausar=True, admin_id=None, motivo="x")
    assert exc.value.codigo == "agent_reserved"
