"""HU-CRM-20 · `GET /pack/commerce/orders`: filtros, paginación, cliente y aislamiento.

La respuesta conserva la forma de lista (la consumen el panel y la APK) y el total de los
pedidos que coinciden viaja en `X-Total-Count`.
"""

from __future__ import annotations

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

URL = "/api/v1/pack/commerce/orders"


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("pedidos")
    tid, _ = await onboard_tenant(
        name=f"Pedidos {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente_con_pedidos(
    tid: uuid.UUID, nombre: str, pedidos: list[tuple[str, int]]
) -> tuple[uuid.UUID, list[str]]:
    """Cliente con un pedido por cada `(estado, días atrás)`; devuelve su id y los números."""
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    numeros: list[str] = []
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre
            )
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id)
            session.add(conv)
            await session.flush()
            for estado, dias in pedidos:
                seq = (
                    await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})
                ).scalar()
                numero = f"OC-{seq:04d}"
                numeros.append(numero)
                session.add(
                    Order(
                        tenant_id=tid,
                        conversation_id=conv.id,
                        customer_id=cliente.id,
                        idempotency_key=uniq("ped"),
                        order_seq=seq,
                        public_order_number=numero,
                        status=estado,
                        total=10,
                        created_at=datetime.now(UTC) - timedelta(days=dias),
                    )
                )
            return cliente.id, numeros


async def test_sin_parametros_conserva_la_forma_de_lista_y_trae_el_cliente(app_client) -> None:
    tid = await _negocio()
    cliente, numeros = await _cliente_con_pedidos(tid, "Ana Gómez", [("abierto", 1), ("entregado", 2)])
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, headers=headers)

    assert r.status_code == 200, r.text
    filas = r.json()
    assert isinstance(filas, list)
    assert [f["public_order_number"] for f in filas] == numeros  # el más reciente primero
    assert {f["customer_id"] for f in filas} == {str(cliente)}
    assert {f["customer_name"] for f in filas} == {"Ana Gómez"}
    assert r.headers["X-Total-Count"] == "2"


async def test_filtra_por_estado(app_client) -> None:
    tid = await _negocio()
    _, numeros = await _cliente_con_pedidos(
        tid, "Ana", [("abierto", 1), ("despachado", 2), ("abierto", 3)]
    )
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"estado": "abierto"}, headers=headers)

    assert [f["public_order_number"] for f in r.json()] == [numeros[0], numeros[2]]
    assert r.headers["X-Total-Count"] == "2"


async def test_estado_fuera_de_catalogo_responde_400(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"estado": "inventado"}, headers=headers)

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "invalid_status"


async def test_busca_por_numero_y_por_nombre_del_cliente(app_client) -> None:
    tid = await _negocio()
    _, de_ana = await _cliente_con_pedidos(tid, "Ana Gómez", [("abierto", 1)])
    _, de_luis = await _cliente_con_pedidos(tid, "Luis Pérez", [("abierto", 2)])
    headers = await api_headers(app_client, tid)

    por_numero = await app_client.get(URL, params={"q": de_luis[0].lower()}, headers=headers)
    por_nombre = await app_client.get(URL, params={"q": "gómez"}, headers=headers)
    sin_coincidencia = await app_client.get(URL, params={"q": "zzz"}, headers=headers)

    assert [f["public_order_number"] for f in por_numero.json()] == de_luis
    assert [f["public_order_number"] for f in por_nombre.json()] == de_ana
    assert sin_coincidencia.json() == []
    assert sin_coincidencia.headers["X-Total-Count"] == "0"


async def test_la_busqueda_trata_comodines_como_texto(app_client) -> None:
    tid = await _negocio()
    await _cliente_con_pedidos(tid, "Ana", [("abierto", 1)])
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"q": "%"}, headers=headers)

    assert r.json() == []


async def test_filtra_por_cliente(app_client) -> None:
    tid = await _negocio()
    ana, de_ana = await _cliente_con_pedidos(tid, "Ana", [("abierto", 1), ("entregado", 2)])
    await _cliente_con_pedidos(tid, "Luis", [("abierto", 3)])
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"customer_id": str(ana)}, headers=headers)

    assert [f["public_order_number"] for f in r.json()] == de_ana
    assert r.headers["X-Total-Count"] == "2"


async def test_pagina_con_limit_y_offset_y_el_total_es_el_de_todos(app_client) -> None:
    tid = await _negocio()
    _, numeros = await _cliente_con_pedidos(tid, "Ana", [("abierto", d) for d in range(1, 6)])
    headers = await api_headers(app_client, tid)

    p1 = await app_client.get(URL, params={"limit": 2, "offset": 0}, headers=headers)
    p3 = await app_client.get(URL, params={"limit": 2, "offset": 4}, headers=headers)

    assert [f["public_order_number"] for f in p1.json()] == numeros[:2]
    assert [f["public_order_number"] for f in p3.json()] == numeros[4:]
    assert p1.headers["X-Total-Count"] == p3.headers["X-Total-Count"] == "5"


async def test_limit_fuera_de_rango_responde_422(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)

    assert (await app_client.get(URL, params={"limit": 0}, headers=headers)).status_code == 422
    assert (await app_client.get(URL, params={"limit": 201}, headers=headers)).status_code == 422
    assert (await app_client.get(URL, params={"offset": -1}, headers=headers)).status_code == 422


async def test_un_negocio_no_ve_pedidos_ni_clientes_de_otro(app_client) -> None:
    mio, otro = await _negocio(), await _negocio()
    await _cliente_con_pedidos(mio, "Ana Gómez", [("abierto", 1)])
    cliente_ajeno, de_ajeno = await _cliente_con_pedidos(otro, "Ana Gómez", [("abierto", 1)])
    headers = await api_headers(app_client, mio)

    todos = await app_client.get(URL, headers=headers)
    por_cliente_ajeno = await app_client.get(
        URL, params={"customer_id": str(cliente_ajeno)}, headers=headers
    )
    por_numero_ajeno = await app_client.get(URL, params={"q": de_ajeno[0]}, headers=headers)

    assert [f["customer_id"] == str(cliente_ajeno) for f in todos.json()] == [False]
    assert todos.headers["X-Total-Count"] == "1"
    assert por_cliente_ajeno.json() == []
    assert por_cliente_ajeno.headers["X-Total-Count"] == "0"
    # El número de pedido es por negocio: el del otro negocio puede coincidir con uno propio,
    # pero nunca devuelve la fila ajena.
    assert all(f["customer_id"] != str(cliente_ajeno) for f in por_numero_ajeno.json())
