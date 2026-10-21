"""HU-UX-16 · Clientes: pausa por cliente, ficha, corrección de datos, filtros y CSV.

Con la BD real: el interruptor cambia `Customer.bot_paused`; el PATCH escribe por
`core/memory/facts.py` como dato de una persona (cierra el hecho del agente); los filtros por
conexión y por clasificación (que aporta pack_commerce) devuelven solo lo que corresponde; nada
cruza negocios.
"""

from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import UTC, datetime, timedelta

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


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("clientes")
    tid, _ = await onboard_tenant(
        name=f"Clientes {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str, *, conexion: uuid.UUID | None = None, pedidos: list[int] | None = None) -> uuid.UUID:
    """Cliente con una conversación (por `conexion`) y pedidos hace N días."""
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre)
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, connection_id=conexion)
            session.add(conv)
            await session.flush()
            for dias in pedidos or []:
                seq = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})).scalar()
                session.add(
                    Order(
                        tenant_id=tid,
                        conversation_id=conv.id,
                        customer_id=cliente.id,
                        idempotency_key=uniq("ped"),
                        order_seq=seq,
                        public_order_number=f"OC-{seq:04d}",
                        total=10,
                        created_at=datetime.now(UTC) - timedelta(days=dias),
                    )
                )
            return cliente.id


async def _conexion(tid: uuid.UUID, label: str) -> uuid.UUID:
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            con = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="manual", label=label, external_id=uniq("pn")
            )
            session.add(con)
            await session.flush()
            return con.id


async def test_pausa_por_cliente_y_aislamiento(app_client) -> None:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    otro = await _negocio()
    ana = await _cliente(tid, "Ana")
    ajeno = await _cliente(otro, "Ajeno")
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{URL}/{ana}/toggle-bot", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"bot_paused": True}
    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await session.get(Customer, ana)).bot_paused is True

    lista = (await app_client.get(URL, headers=headers)).json()
    assert {i["id"]: i["bot_paused"] for i in lista["items"]}[str(ana)] is True

    r = await app_client.post(f"{URL}/{ana}/toggle-bot", headers=headers)
    assert r.json() == {"bot_paused": False}

    # El cliente de otro negocio no existe para este.
    assert (await app_client.post(f"{URL}/{ajeno}/toggle-bot", headers=headers)).status_code == 404
    assert (await app_client.get(f"{URL}/{ajeno}", headers=headers)).status_code == 404
    assert (await app_client.patch(f"{URL}/{ajeno}", json={"datos": {"ciudad": "X"}}, headers=headers)).status_code == 404


async def test_ficha_y_correccion_como_dato_de_persona(app_client) -> None:
    from core.db.session import get_session
    from core.memory import facts
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana = await _cliente(tid, "Ana")
    async with tenant_scope(tid):
        async with get_session() as session:
            await facts.escribir_hechos(
                session, tenant_id=tid, customer_id=ana, namespace=facts.PERFIL,
                valores={"direccion": "Calle 1", "documento": "123"}, source="tool",
            )
    headers = await api_headers(app_client, tid)

    ficha = (await app_client.get(f"{URL}/{ana}", headers=headers)).json()
    datos = {d["key"]: d for d in ficha["datos"]}
    assert datos["direccion"]["value"] == "Calle 1"
    assert datos["direccion"]["source"] == "tool"
    assert "clasificacion" in {c["key"] for c in ficha["columns"]}
    assert ficha["extras"]["clasificacion"] == "Nuevo"

    r = await app_client.patch(
        f"{URL}/{ana}", json={"datos": {"direccion": "Carrera 7", "name": "Ana María"}}, headers=headers
    )
    assert r.status_code == 200, r.text
    ficha = r.json()
    datos = {d["key"]: d for d in ficha["datos"]}
    assert datos["direccion"]["value"] == "Carrera 7"
    assert datos["direccion"]["source"] == "human"
    assert datos["documento"]["source"] == "tool"  # lo que no se tocó sigue igual
    assert ficha["nombre"] == "Ana María"

    # La proyección que lee el agente (`customers.attributes`) quedó al día.
    from core.db.models import Customer

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = await session.get(Customer, ana)
            assert cliente.attributes["direccion"] == "Carrera 7"

    r = await app_client.patch(f"{URL}/{ana}", json={"datos": {"crm_lead_id": "x"}}, headers=headers)
    assert r.status_code == 400
    assert "crm_lead_id" in r.json()["error"]["fields"]
    r = await app_client.patch(f"{URL}/{ana}", json={"datos": {"name": "  "}}, headers=headers)
    assert r.status_code == 400
    r = await app_client.patch(f"{URL}/{ana}", json={"datos": {"tarjeta": "4111"}}, headers=headers)
    assert r.status_code == 400
    assert "tarjeta" in r.json()["error"]["fields"]


async def test_filtros_por_conexion_y_clasificacion(app_client) -> None:
    tid = await _negocio()
    tienda = await _conexion(tid, "Tienda")
    soporte = await _conexion(tid, "Soporte")
    nuevo = await _cliente(tid, "Nuevo", conexion=tienda)
    fiel = await _cliente(tid, "Fiel", conexion=soporte, pedidos=[50, 40, 30, 20, 10])
    inactivo = await _cliente(tid, "Inactivo", conexion=tienda, pedidos=[200])
    ocasional = await _cliente(tid, "Ocasional", pedidos=[5])
    headers = await api_headers(app_client, tid)

    async def ids(**params) -> set[str]:
        r = await app_client.get(URL, params={**params, "page_size": 100}, headers=headers)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert await ids(connection_id=str(tienda)) == {str(nuevo), str(inactivo)}
    assert await ids(connection_id=str(soporte)) == {str(fiel)}
    assert await ids(filtro="clasificacion:nuevo") == {str(nuevo)}
    assert await ids(filtro="clasificacion:fiel") == {str(fiel)}
    assert await ids(filtro="clasificacion:inactivo") == {str(inactivo)}
    assert await ids(filtro="clasificacion:ocasional") == {str(ocasional)}
    assert await ids(filtro="clasificacion:nuevo", connection_id=str(soporte)) == set()

    cuerpo = (await app_client.get(URL, headers=headers)).json()
    assert {f["key"] for f in cuerpo["filters"]} >= {"clasificacion", "estado_lead"}
    por_id = {i["id"]: i["extras"]["clasificacion"] for i in cuerpo["items"]}
    assert por_id[str(fiel)] == "Fiel" and por_id[str(inactivo)] == "Inactivo"

    assert (await app_client.get(URL, params={"filtro": "clasificacion:vip"}, headers=headers)).status_code == 400
    assert (await app_client.get(URL, params={"filtro": "saldo:1"}, headers=headers)).status_code == 400


async def test_exporta_csv_filtrado(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "=HYPERLINK(\"x\")")
    await _cliente(tid, "Fiel", pedidos=[50, 40, 30, 20, 10])
    headers = await api_headers(app_client, tid)

    r = await app_client.get(f"{URL}/export.csv", params={"filtro": "clasificacion:nuevo"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    filas = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert filas[0][:5] == ["Cliente", "Canal", "Teléfono", "Agente", "Alta"]
    assert [f[0] for f in filas[1:]] == ["'=HYPERLINK(\"x\")"]
