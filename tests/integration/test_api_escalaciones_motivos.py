"""HU-UX-17 · `GET /api/v1/admin/escalations`: `reason_code`, `reason_label` y `?reason_code=`.

Con la BD real: el filtro SQL clasifica igual que `motivos_escalacion.codigo` (también los
motivos viejos con texto libre), no cruza negocios y un código fuera del catálogo da 422.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/escalations"

MOTIVOS = [
    ("tool:queja", "Llegó tarde"),
    ("tool:monto_alto", ""),
    ("tool:otro", "Detalle del motivo: factura a otro NIT"),
    ("tool:el cliente quiere hablar con el gerente", ""),
    ("tool:solicitudXcliente", ""),
    ("keyword", "quiero un asesor"),
    ("llm_error", "Fallo del proveedor LLM"),
    ("plugin:crear_pedido", ""),
    ("guardrail", "regla"),
    ("guardrail", "precio_inventado"),
    ("send_failed", ""),
    ("turns_unresolved", "3 turnos sin resolver"),
    ("motivo_que_no_existe", ""),
]


async def _negocio_con_escalaciones(motivos: list[tuple[str, str]]) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.db.models import Conversation, Customer, Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("motivos")
    tid, _ = await onboard_tenant(
        name=f"Motivos {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local"
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            for reason, summary in motivos:
                cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
                session.add(cliente)
                await session.flush()
                conv = Conversation(tenant_id=tid, customer_id=cliente.id, status="escalated")
                session.add(conv)
                await session.flush()
                session.add(Escalation(tenant_id=tid, conversation_id=conv.id, reason=reason, summary=summary))
    return tid


async def test_codigo_etiqueta_y_filtro_coinciden(app_client) -> None:
    from core.orchestrator import motivos_escalacion as m

    tid = await _negocio_con_escalaciones(MOTIVOS)
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"page_size": 100}, headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == len(MOTIVOS)
    por_reason = {(i["reason"], i["summary"]): i for i in items}
    assert por_reason[("tool:queja", "Llegó tarde")]["reason_label"] == "Queja"
    assert por_reason[("keyword", "quiero un asesor")]["reason_code"] == "palabra_clave"
    legado = por_reason[("tool:el cliente quiere hablar con el gerente", "")]
    assert (legado["reason_code"], legado["reason_label"]) == ("otro", "Otro")
    assert por_reason[("plugin:crear_pedido", "")]["reason_label"].startswith("Acción de ")
    assert por_reason[("guardrail", "regla")]["reason_code"] == "regla_negocio"

    for cod in m.MOTIVOS:
        r = await app_client.get(URL, params={"reason_code": cod, "page_size": 100}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        got = sorted((i["reason"], i["summary"]) for i in body["items"])
        esperado = sorted((rs, s) for rs, s in MOTIVOS if m.codigo(rs, s) == cod)
        assert got == esperado, cod
        assert body["total"] == len(esperado), cod
        assert all(i["reason_code"] == cod for i in body["items"])


async def test_filtro_no_cruza_negocios(app_client) -> None:
    tid_a = await _negocio_con_escalaciones([("tool:queja", "A")])
    await _negocio_con_escalaciones([("tool:queja", "B")])

    r = await app_client.get(URL, params={"reason_code": "queja"}, headers=await api_headers(app_client, tid_a))
    assert r.status_code == 200, r.text
    assert [i["summary"] for i in r.json()["items"]] == ["A"]


async def test_codigo_fuera_del_catalogo_da_422(app_client) -> None:
    tid = await _negocio_con_escalaciones([])
    r = await app_client.get(URL, params={"reason_code": "tool:queja"}, headers=await api_headers(app_client, tid))
    assert r.status_code == 422


async def test_filtro_por_estado(app_client) -> None:
    from test_backfill_agentes import _sql

    tid = await _negocio_con_escalaciones([("tool:queja", "Pendiente"), ("tool:queja", "Resuelta")])
    await _sql(
        tid,
        "update escalations set status = 'resuelta' where id = "
        "(select id from escalations where tenant_id = :t order by created_at desc limit 1)",
        t=tid,
    )
    headers = await api_headers(app_client, tid)
    pendientes = (await app_client.get(URL, params={"estado": "pendiente"}, headers=headers)).json()
    resueltas = (await app_client.get(URL, params={"estado": "resuelta"}, headers=headers)).json()
    assert [i["summary"] for i in pendientes["items"]] == ["Pendiente"]
    assert [i["summary"] for i in resueltas["items"]] == ["Resuelta"]
    assert (await app_client.get(URL, params={"estado": "otro"}, headers=headers)).status_code == 422


async def test_trae_el_cliente_de_la_escalacion_y_no_el_de_otro_negocio(app_client) -> None:
    """HU-CRM-20: `customer_id` es el de la conversación; el listado nunca trae el de otro negocio."""
    from sqlalchemy import select

    from core.db.models import Conversation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    mio = await _negocio_con_escalaciones([("tool:queja", "Llegó tarde")])
    otro = await _negocio_con_escalaciones([("tool:queja", "Del otro negocio")])
    headers = await api_headers(app_client, mio)

    r = await app_client.get(URL, headers=headers)

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["summary"] for i in items] == ["Llegó tarde"]
    async with tenant_scope(mio):
        async with get_session() as session:
            conv = (await session.execute(select(Conversation))).scalars().one()
    assert items[0]["customer_id"] == str(conv.customer_id)
    async with tenant_scope(otro):
        async with get_session() as session:
            ajeno = (await session.execute(select(Conversation))).scalars().one()
    assert items[0]["customer_id"] != str(ajeno.customer_id)
