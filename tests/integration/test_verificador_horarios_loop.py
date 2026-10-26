"""HU-AOS-20 parte 3 · horarios y preguntas con el loop real y un modelo guionado (sin LLM).

Negocio v3 con el motor de procedimientos (Ventas y Agenda, como el test de enrutamiento):

- **Horarios en `active`:** una hora inventada se rechaza (`horario_sin_evidencia`), el reintento lleva
  la sección del playbook `verificador-estado` y sale la respuesta corregida.
- **Horarios en `shadow`** (por defecto): pasa y queda `unverifiable` en `turn_outcomes.verifier`.
- **Preguntas en `active`:** dos preguntas se rechazan (`demasiadas_preguntas`).
- **Bandera del master:** `verificar_horarios = active` ya se puede encender (200).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers
from test_enrutamiento_especialistas import (
    _con_dos_especialistas,
    _conversacion,
    _Guion,
    _mensaje,
    _sql,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

INVENTADA = "Te agendo el jueves a las 7 de la noche."


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


class _GuionConMensajes(_Guion):
    def __init__(self, pasos: list[Any]) -> None:
        super().__init__(pasos)
        self.ultimos: list[str] = []

    async def generate(self, **kwargs: Any) -> Any:
        mensajes = kwargs.get("messages") or []
        self.ultimos.append(str(getattr(mensajes[-1], "content", "")) if mensajes else "")
        return await super().generate(**kwargs)


async def _turno(tid: uuid.UUID, conv: uuid.UUID, monkeypatch: Any, pasos: list[Any], texto: str) -> _GuionConMensajes:
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    guion = _GuionConMensajes(pasos)
    await _mensaje(tid, conv)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(tid):
        await process_conversation(conv, texto)
    await _mensaje(tid, conv, direccion="outbound")
    return guion


async def _verificar(tid: uuid.UUID, **cambios: str) -> None:
    from core.kernel import banderas_pila
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await banderas_pila.fijar(tid, cambios, actor="test")


async def _veredicto(tid: uuid.UUID, conv: uuid.UUID) -> list[dict[str, Any]]:
    ((verifier,),) = await _sql(
        tid,
        "select o.verifier from turn_outcomes o join core_decisions d on d.tenant_id = o.tenant_id "
        "and d.id = o.turn_id where d.conversation_id = :c order by d.created_at desc limit 1",
        c=conv,
    )
    return list(verifier or [])


def _seccion(clave: str) -> str:
    from core.orchestrator.verificador.verificador import _instruccion_del_kernel

    return _instruccion_del_kernel(clave)


async def test_horario_inventado_en_active_se_corrige(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    await _verificar(tid, verificar_horarios="active")
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, [INVENTADA, "¿Qué día te sirve para la demo?"], texto="hola")
    assert len(g.ultimos) == 2
    assert _seccion("verificador_horario_sin_evidencia")[:40] in g.ultimos[1]
    veredicto = await _veredicto(tid, conv)
    assert {"kind": "horario_sin_evidencia", "verdict": "fail", "count": 1} in veredicto, veredicto


async def test_horario_en_shadow_solo_cuenta(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, [INVENTADA], texto="hola")
    assert len(g.ultimos) == 1
    veredicto = await _veredicto(tid, conv)
    assert any(v["kind"] == "horarios_sin_evidencia" and v["verdict"] == "unverifiable" for v in veredicto), veredicto


async def test_dos_preguntas_en_active_se_corrigen(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    await _verificar(tid, verificar_preguntas="active")
    conv = await _conversacion(tid, cid)
    g = await _turno(
        tid, conv, monkeypatch, ["¿Prefieres el plan mensual o el anual? ¿Te agendo también la demo?", "¿Mensual o anual?"],
        texto="hola",
    )
    assert len(g.ultimos) == 2 and _seccion("verificador_demasiadas_preguntas")[:40] in g.ultimos[1]
    assert {"kind": "demasiadas_preguntas", "verdict": "fail", "count": 1} in await _veredicto(tid, conv)


async def test_el_master_enciende_la_verificacion(app_client) -> None:
    tid, _cid, _inst = await _con_dos_especialistas("off")
    master = await api_headers(app_client, tid)
    url = f"/api/v1/admin/platform/tenants/{tid}/colaboracion"
    r = await app_client.put(url, headers=master, json={"verificar_horarios": "active", "verificar_preguntas": "active"})
    assert r.status_code == 200 and r.json()["verificar_horarios"] == "active", r.text
