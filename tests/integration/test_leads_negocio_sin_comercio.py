"""HU-AOS-70 · Clientes de un negocio sin comercio: el lead con su etapa, origen, seguimiento y negocio.

Con la BD real: en un negocio solo con Suscripciones, `/admin/leads?agente=leads` trae las columnas del lead del core
(etapa, origen, próxima acción y negocio) y ninguna de comercio (pedidos, cotizaciones); el negocio que guardó
`actualizar_lead` (en `custom`) sale en su columna y basta para que el lead cuente «con datos»; otro negocio no lo ve.
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

URL = "/api/v1/admin/leads"


async def _negocio(packs: list[str]) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("negsc")
    tid, _ = await onboard_tenant(
        name=f"Sin comercio {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=packs, sembrar_ejemplos=False,
    )
    return tid


async def _cliente_con_lead(tid: uuid.UUID, **campos) -> uuid.UUID:
    from core.db.models import Customer, Lead
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
            session.add(cliente)
            await session.flush()
            session.add(Lead(tenant_id=tid, customer_id=cliente.id, **campos))
            return cliente.id


async def test_lista_del_lead_sin_comercio_trae_su_negocio_y_ninguna_columna_de_comercio(app_client) -> None:
    tid = await _negocio(["pack_subscriptions"])
    cliente = await _cliente_con_lead(
        tid, status="contactado", origen="whatsapp", custom={"negocio": "Panadería La Espiga"}, next_action_note="Llamar"
    )
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"agente": "leads"}, headers=headers)
    assert r.status_code == 200, r.text
    leads = next(a for a in r.json()["agents"] if a["key"] == "leads")
    claves = {c["key"] for c in leads["columns"]}
    assert {"estado_lead", "origen", "seguimiento_nota", "negocio"} <= claves
    todas = {c["key"] for a in r.json()["agents"] for c in a["columns"]}
    assert not any("pedido" in k or "cotiz" in k or "order" in k or "quote" in k for k in todas), todas
    fila = next(i for i in r.json()["items"] if i["id"] == str(cliente))
    assert fila["extras"]["negocio"] == "Panadería La Espiga"
    assert fila["extras"]["origen"]

    otro = await _negocio(["pack_subscriptions"])
    r = await app_client.get(URL, params={"agente": "leads"}, headers=await api_headers(app_client, otro))
    assert r.status_code == 200, r.text
    assert all(i["id"] != str(cliente) for i in r.json()["items"])
