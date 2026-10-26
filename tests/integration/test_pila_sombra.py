"""HU-AOS-20 parte 1 · la pila de tareas en sombra con el loop real y un modelo guionado (sin LLM).

Negocio v3 con Ventas (por defecto, `contratar_plan`) y Agenda (`agendar_cita`), como el test de
enrutamiento de HU-AOS-17:

- **Sombra con el ruteo activo:** «quiero el plan premium y una demo el jueves» deja en
  `core_decisions.shadow_route.pila` lo que abriría (plan en foco, demo en pausa) y no crea tareas.
- **Digresión con foco:** con la tarea de Ventas en foco, «… y también una demo» anota la demo en
  pausa y el turno sigue con Ventas.
- **Apagada** (por defecto) o **ruteo apagado:** no hay clave `pila`.
- **Bandera del master:** `PUT .../colaboracion` cambia solo `settings.agent_os`, se audita, rechaza lo
  que todavía no está disponible (409) y un dueño no la toca (403).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_enrutamiento_especialistas import (
    CITA,
    PLAN,
    _con_dos_especialistas,
    _conversacion,
    _decision,
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

DOS = "quiero el plan premium y una demo el jueves"


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _pila(tid: uuid.UUID, modo: str) -> None:
    from core.kernel import banderas_pila as colaboracion
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await colaboracion.fijar(tid, {"pila": modo}, actor="test")


async def test_sombra_con_ruteo_activo_anota_la_pila_sin_crear_tareas(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    await _pila(tid, "shadow")
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["¿Qué plan te interesa?"], texto=DOS)
    d = await _decision(tid, conv)
    assert d["shadow_route"] == {"pila": {"abre": [PLAN], "pausa": [CITA], "sobrantes": []}}
    assert await _sql(tid, "select count(*) from conversation_tasks where conversation_id = :c", c=conv) == [(0,)]


async def test_digresion_con_foco_anota_la_pausa(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_dos_especialistas("active")
    await _pila(tid, "shadow")
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, [("iniciar_procedimiento", {"procedure_id": PLAN}), "¿Qué plan?"], texto="quiero el plan premium")
    await _turno(tid, conv, monkeypatch, ["Anotado."], texto="el premium, y también quiero agendar una demo")
    d = await _decision(tid, conv)
    assert d["route_source"] == "task" and d["active_instance_id"] == str(inst["ventas"])
    assert d["shadow_route"] == {"pila": {"abre": [], "pausa": [CITA], "sobrantes": []}}


@pytest.mark.parametrize(("pila", "ruteo"), [("off", "active"), ("shadow", "off")])
async def test_sin_pila_o_sin_ruteo_no_hay_clave(_ruteo: None, monkeypatch: Any, pila: str, ruteo: str) -> None:
    tid, cid, _inst = await _con_dos_especialistas(ruteo)
    await _pila(tid, pila)
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["¿Qué plan te interesa?"], texto=DOS)
    d = await _decision(tid, conv)
    assert not (d["shadow_route"] or {}).get("pila")


async def test_la_bandera_la_cambia_solo_el_master(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid, _cid, _inst = await _con_dos_especialistas("off")
    await _sql(tid, "update tenants set settings = coalesce(settings,'{}'::jsonb) || '{\"evals\": {\"gate\": \"cerrado\"}}' where id = :t", t=tid)
    master = await api_headers(app_client, tid)
    url = f"/api/v1/admin/platform/tenants/{tid}/colaboracion"
    r = await app_client.get(url, headers=master)
    assert r.status_code == 200 and r.json()["pila"] == "off" and r.json()["verificar_horarios"] == "shadow"
    r = await app_client.put(url, headers=master, json={"pila": "shadow"})
    assert r.status_code == 200 and r.json()["pila"] == "shadow", r.text
    ((agent_os, evals),) = await _sql(tid, "select settings->'agent_os', settings->'evals' from tenants where id = :t", t=tid)
    assert agent_os == {"pila": "shadow"} and evals == {"gate": "cerrado"}
    # Un valor fuera del contrato es 422 (todo lo de la HU ya se puede encender).
    assert (await app_client.put(url, headers=master, json={"pila": "turbo"})).status_code == 422
    assert await _sql(tid, "select count(*) from audit_log where action = 'agent_os_colaboracion'") == [(1,)]

    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    dueno = await api_headers(app_client, tid, email=email, password="clave-segura-123")
    assert (await app_client.put(url, headers=dueno, json={"pila": "off"})).status_code == 403


async def test_aislamiento_entre_negocios(_ruteo: None, monkeypatch: Any) -> None:
    """Las tareas abiertas, la sombra y la bandera de un negocio no se ven ni cambian en otro."""
    from core.db.session import get_session
    from core.kernel import banderas_pila
    from core.kernel.router import procedimientos_abiertos
    from core.kernel.ruteo_sombra import reporte_de_pila
    from core.shared.tenant import tenant_scope

    a, cid_a, _ = await _con_dos_especialistas("active")
    b, cid_b, _ = await _con_dos_especialistas("active")
    await _pila(a, "shadow")
    await _pila(b, "shadow")
    conv_b = await _conversacion(b, cid_b)
    await _turno(b, conv_b, monkeypatch, [("iniciar_procedimiento", {"procedure_id": PLAN}), "¿Qué plan?"], texto="quiero el plan premium")
    conv_a = await _conversacion(a, cid_a)
    await _turno(a, conv_a, monkeypatch, ["¿Qué plan te interesa?"], texto=DOS)
    async with tenant_scope(a):
        async with get_session() as session:
            # La conversación de B leída bajo el scope de A: nada (filtro por negocio y RLS).
            assert await procedimientos_abiertos(session, a, conv_b) == ()
        reporte = await reporte_de_pila(a)
        assert reporte.turnos == 1
        await banderas_pila.fijar(a, {"pila": "off"}, actor="test")
    async with tenant_scope(b):
        async with get_session() as session:
            assert await procedimientos_abiertos(session, b, conv_b) == (PLAN,)
        assert (await banderas_pila.leer(b)).pila == "shadow"
        assert (await reporte_de_pila(b)).turnos == 0  # su turno tenía foco nuevo, no pila

