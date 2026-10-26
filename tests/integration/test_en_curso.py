"""HU-AOS-28 · «En curso» y la toma humana (ADR-0169 §5).

- `GET /admin/conversations/{id}/in-progress`: tareas abiertas y, en solo lectura, las cerradas como
  `escalated` desde la escalación activa; datos dados (pagos enmascarados), lo que falta estructurado
  sobre la definición EXACTA (`definicion_no_disponible` si no está) y `sin_procedimientos` sin tareas;
- `take` escribe `taken` una sola vez en cada tarea abierta (no en las cerradas); devolverla al agente
  escribe `released`;
- aislamiento: otra conversación de otro negocio y el playground dan 404.

Sin LLM: las tareas se crean con `state.iniciar` / `state.registrar` sobre procedimientos del binario.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers
from test_canary import _conversacion_unica
from test_releases_por_conexion import _negocio, _sql

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLAN = "pack_commerce.contratar_plan"
PEDIDO = "pack_commerce.tomar_pedido"


@pytest.fixture(autouse=True)
def _procedimientos_de_comercio() -> None:
    """El host de los tests no activa los packs: se registran sus procedimientos (como en
    `test_procedimientos_agenda_subs_cobranza`)."""
    from pack_commerce.procedimientos import registrar
    from pack_commerce.skills import CatalogSkill, LeadSkill, OrderSkill

    from core.mcp.host import mcp_host

    if "pack_commerce" in mcp_host.get_procedures():
        return
    mcp_host.remove_plugin("pack_commerce")
    mcp_host.begin_activation("pack_commerce")
    try:
        for skill in (LeadSkill, CatalogSkill, OrderSkill):
            mcp_host.register_skill(skill())
        registrar(mcp_host)
    finally:
        mcp_host.end_activation()


async def _en_scope(tid: uuid.UUID, fn: Any) -> Any:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await fn(s)


async def _iniciar(tid: uuid.UUID, conv: uuid.UUID, proc: str, version: str, stage: str) -> uuid.UUID:
    from core.kernel import state

    async def _f(s: Any) -> uuid.UUID:
        fila = await state.iniciar(
            s, tenant_id=tid, conversation_id=conv, procedure_id=proc, procedure_version=version, stage=stage, actor="kernel"
        )
        return fila.id

    return await _en_scope(tid, _f)


async def _registrar(tid: uuid.UUID, conv: uuid.UUID, task: uuid.UUID, tipo: str, actor: str = "kernel", **data: Any) -> None:
    from core.kernel import state

    async def _f(s: Any) -> None:
        await state.registrar(s, tenant_id=tid, conversation_id=conv, task_id=task, tipo=tipo, data=data, actor=actor)

    await _en_scope(tid, _f)


async def _escalar(tid: uuid.UUID, conv: uuid.UUID) -> None:
    await _sql(
        tid,
        "insert into escalations (id, tenant_id, conversation_id, reason, summary, status) "
        "values (:id, :t, :c, 'cliente_pide_humano', 'Quiere hablar con alguien', 'pendiente')",
        id=uuid.uuid4(), t=tid, c=conv,
    )


async def _eventos(tid: uuid.UUID, task: uuid.UUID, tipo: str) -> list[Any]:
    return await _sql(tid, "select actor, data from task_events where task_id = :k and type = :ty order by seq", k=task, ty=tipo)


async def _escenario() -> dict[str, Any]:
    tid, _slug, cid = await _negocio()
    conv = await _conversacion_unica(tid, cid)
    await _escalar(tid, conv)
    # La tarea que el motor cerró como `escalated` justo antes de escalar.
    pedido = await _iniciar(tid, conv, PEDIDO, "1.1.0", "armar")
    await _registrar(tid, conv, pedido, "escalated", reason="action_error")
    plan = await _iniciar(tid, conv, PLAN, "1.3.0", "elegir_plan")
    await _registrar(tid, conv, plan, "slot_set", actor="llm", slot="full_name", value="Ana Pérez", source="llm", data_class="pii")
    await _registrar(tid, conv, plan, "slot_set", actor="llm", slot="email", value="ana@correo.co", source="llm", data_class="pii")
    await _registrar(tid, conv, plan, "slot_set", actor="kernel", slot="quote_id", value="WOMPI-REF-4821", source="llm", data_class="payment")
    return {"tid": tid, "cid": cid, "conv": conv, "plan": plan, "pedido": pedido}


async def test_en_curso_dice_que_falta_e_incluye_la_tarea_escalada(app_client) -> None:
    e = await _escenario()
    h = await api_headers(app_client, e["tid"])
    r = await app_client.get(f"/api/v1/admin/conversations/{e['conv']}/in-progress", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["escalation"]["reason_code"] and d["escalation"]["status"] == "pendiente" and d["empty_reason"] is None
    plan, pedido = d["tasks"]
    assert plan["task_id"] == str(e["plan"]) and plan["in_focus"] and plan["title"]
    assert {x["name"]: x["value"] for x in plan["data"]} == {
        "full_name": "Ana Pérez", "email": "ana@correo.co", "quote_id": "•••",  # pago: siempre oculto
    }
    faltan = {x["name"] for x in plan["missing"]}
    assert faltan == {"variant_id"} and plan["missing_reason"] is None
    assert all(x["label"] for x in plan["missing"])
    assert pedido["task_id"] == str(e["pedido"]) and pedido["status"] == "escalated" and pedido["status_reason"] == "action_error"


async def test_tomar_anota_una_vez_en_las_abiertas_y_devolver_suelta(app_client) -> None:
    e = await _escenario()
    h = await api_headers(app_client, e["tid"])
    base = f"/api/v1/admin/conversations/{e['conv']}"
    assert (await app_client.post(f"{base}/take", headers=h)).status_code == 200
    assert (await app_client.post(f"{base}/take", headers=h)).status_code == 200  # idempotente
    tomadas = await _eventos(e["tid"], e["plan"], "taken")
    assert len(tomadas) == 1 and tomadas[0][0] == "human" and uuid.UUID(tomadas[0][1]["admin_id"])
    assert await _eventos(e["tid"], e["pedido"], "taken") == []  # cerrada: no se anota
    # La anotación no cambia la tarea (sigue en la misma etapa y con sus datos).
    assert await _sql(e["tid"], "select status, stage from conversation_tasks where id = :k", k=e["plan"]) == [("active", "elegir_plan")]
    assert (await app_client.post(f"{base}/return-to-bot", headers=h)).status_code == 204
    assert len(await _eventos(e["tid"], e["plan"], "released")) == 1


async def test_sin_definicion_exacta_y_sin_tareas(app_client) -> None:
    tid, _slug, cid = await _negocio()
    conv = await _conversacion_unica(tid, cid)
    h = await api_headers(app_client, tid)
    url = f"/api/v1/admin/conversations/{conv}/in-progress"
    assert (await app_client.get(url, headers=h)).json() == {"tasks": [], "escalation": None, "empty_reason": "sin_procedimientos"}
    await _iniciar(tid, conv, PLAN, "9.9.9", "datos")  # una versión que ni la release ni el binario traen
    tarea = (await app_client.get(url, headers=h)).json()["tasks"][0]
    assert tarea["missing"] == [] and tarea["missing_reason"] == "definicion_no_disponible" and tarea["title"] is None


async def test_aislamiento_y_playground(app_client) -> None:
    from core.shared.test_mode import TEST_CHANNEL

    e = await _escenario()
    otro, _s, _c = await _negocio()
    url = f"/api/v1/admin/conversations/{e['conv']}/in-progress"
    assert (await app_client.get(url, headers=await api_headers(app_client, otro))).status_code == 404
    await _sql(e["tid"], "update conversations set channel = :canal where id = :c", canal=TEST_CHANNEL, c=e["conv"])
    assert (await app_client.get(url, headers=await api_headers(app_client, e["tid"]))).status_code == 404


async def test_otro_negocio_no_toma_ni_suelta_las_tareas(app_client) -> None:
    e = await _escenario()
    otro, _s, _c = await _negocio()
    ho = await api_headers(app_client, otro)
    base = f"/api/v1/admin/conversations/{e['conv']}"
    for accion in ("take", "return-to-bot", "resolve"):
        assert (await app_client.post(f"{base}/{accion}", headers=ho)).status_code == 404
    assert await _eventos(e["tid"], e["plan"], "taken") == [] and await _eventos(e["tid"], e["plan"], "released") == []
