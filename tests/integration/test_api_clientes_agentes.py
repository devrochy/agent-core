"""Clientes por agente: columnas de Ventas, Cobranza y Agenda (tandas 2-4)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

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

    slug = uniq("agentes")
    tid, _ = await onboard_tenant(
        name=f"Agentes {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce", "pack_cobranza", "pack_calendar"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente_con_data(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    from pack_calendar.models import Appointment, AppointmentType
    from pack_cobranza.models import Cartera, Cliente, Cuota, Pago, Prestamo, Promesa
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana"
            )
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id)
            session.add(conv)
            await session.flush()

            seq = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})).scalar()
            session.add(
                Order(
                    tenant_id=tid,
                    conversation_id=conv.id,
                    customer_id=cliente.id,
                    idempotency_key=uniq("ped"),
                    order_seq=seq,
                    public_order_number=f"OC-{seq:04d}",
                    total=Decimal("120000"),
                    status="abierto",
                )
            )

            cartera = Cartera(tenant_id=tid, nombre="Cartera Norte")
            session.add(cartera)
            await session.flush()
            cobranza = Cliente(
                tenant_id=tid,
                cartera_id=cartera.id,
                customer_id=cliente.id,
                nombre="Ana",
                color_riesgo="azul",
                tope_maximo_deuda=Decimal("300000"),
            )
            session.add(cobranza)
            await session.flush()
            prestamo = Prestamo(
                tenant_id=tid,
                cliente_id=cobranza.id,
                cartera_id=cartera.id,
                valor=Decimal("100000"),
                saldo=Decimal("80000"),
                num_cuotas=2,
                tipo_interes=Decimal("0"),
                dias_entre_cuotas=30,
                fecha_otorgado=date.today() - timedelta(days=40),
            )
            session.add(prestamo)
            await session.flush()
            session.add(
                Cuota(
                    tenant_id=tid,
                    prestamo_id=prestamo.id,
                    numero_cuota=1,
                    valor_esperado=Decimal("80000"),
                    fecha_vencimiento=date.today() - timedelta(days=10),
                    estatus="atrasada",
                )
            )
            session.add(
                Promesa(
                    tenant_id=tid,
                    prestamo_id=prestamo.id,
                    cliente_id=cobranza.id,
                    fecha_prometida=date.today() + timedelta(days=3),
                    valor_prometido=Decimal("40000"),
                    estado="pendiente",
                )
            )
            session.add(
                Pago(
                    tenant_id=tid,
                    cliente_id=cobranza.id,
                    valor=Decimal("20000"),
                    fecha_hora=datetime.now(UTC) - timedelta(days=2),
                )
            )

            tipo = AppointmentType(tenant_id=tid, name="Demo", duration_minutes=30)
            session.add(tipo)
            await session.flush()
            inicio = datetime.now(UTC) + timedelta(days=1)
            session.add(
                Appointment(
                    tenant_id=tid,
                    customer_id=cliente.id,
                    conversation_id=conv.id,
                    appointment_type_id=tipo.id,
                    title="Demo",
                    start_at=inicio,
                    end_at=inicio + timedelta(minutes=30),
                    status="scheduled",
                )
            )
            await session.flush()
            return cliente.id, conv.id


async def test_las_tres_areas_aportan_sus_columnas(app_client) -> None:
    tid = await _negocio()
    cliente_id, _ = await _cliente_con_data(tid)
    headers = await api_headers(app_client, tid)

    lista = (await app_client.get(URL, headers=headers)).json()
    assert {"ventas", "cobranza", "agenda"} <= {a["key"] for a in lista["agents"]}
    fila = next(i for i in lista["items"] if i["id"] == str(cliente_id))
    assert {"ventas", "cobranza", "agenda"} <= set(fila["agentes"])
    assert fila["extras"]["pedidos"] == 1
    assert fila["extras"]["total_compras"] == 120000
    assert fila["extras"]["saldo"] == 80000
    assert fila["extras"]["cartera"] == "Cartera Norte"
    assert fila["extras"]["riesgo"] == "Azul"
    assert fila["extras"]["citas"] == 1
    assert fila["extras"]["tipo_frecuente"] == "Demo"

    ficha = (await app_client.get(f"{URL}/{cliente_id}", headers=headers)).json()
    por_agente = {a["key"]: a for a in ficha["agentes"]}
    ventas = {c["key"]: c["value"] for c in por_agente["ventas"]["columns"]}
    cobranza = {c["key"]: c["value"] for c in por_agente["cobranza"]["columns"]}
    agenda = {c["key"]: c["value"] for c in por_agente["agenda"]["columns"]}
    assert ventas["pedidos"] == 1 and ventas["total_compras"] == 120000
    assert cobranza["saldo"] == 80000 and cobranza["mora"] == 10
    assert agenda["citas"] == 1 and agenda["proxima_cita"]


async def test_edita_cobranza_y_deja_auditoria(app_client) -> None:
    from pack_cobranza.models import AuditoriaCartera, Cliente
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente_id, _ = await _cliente_con_data(tid)
    headers = await api_headers(app_client, tid)

    r = await app_client.patch(
        f"{URL}/{cliente_id}/pack/pack_cobranza",
        headers=headers,
        json={"valores": {"color_riesgo": "rojo", "tope_maximo_deuda": "500000", "estatus": "inactivo"}},
    )
    assert r.status_code == 200, r.text
    editables = {e["key"]: e["value"] for e in r.json()["editables"] if e["plugin_id"] == "pack_cobranza"}
    assert editables == {"color_riesgo": "rojo", "tope_maximo_deuda": "500000.00", "estatus": "inactivo"}

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = (
                await session.execute(select(Cliente).where(Cliente.customer_id == cliente_id))
            ).scalars().one()
            assert (cliente.color_riesgo, cliente.estatus, str(cliente.tope_maximo_deuda)) == (
                "rojo",
                "inactivo",
                "500000.00",
            )
            auditorias = (
                await session.execute(
                    select(AuditoriaCartera).where(AuditoriaCartera.entidad_id == cliente.id)
                )
            ).scalars().all()
            assert {a.operacion for a in auditorias} == {"editar"}
            assert len(auditorias) == 3

    r = await app_client.patch(
        f"{URL}/{cliente_id}/pack/pack_cobranza", headers=headers, json={"valores": {"color_riesgo": "violeta"}}
    )
    assert r.status_code == 400
    assert "color_riesgo" in r.json()["error"]["fields"]


async def _cliente_solo(tid: uuid.UUID, *, con_pedido: bool, nombre: str = "Solo") -> uuid.UUID:
    """Un cliente de WhatsApp; con `con_pedido` agrega un pedido y nada más."""
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('s')}", name=nombre
            )
            session.add(cliente)
            await session.flush()
            if con_pedido:
                conv = Conversation(tenant_id=tid, customer_id=cliente.id)
                session.add(conv)
                await session.flush()
                seq = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})).scalar()
                session.add(
                    Order(
                        tenant_id=tid,
                        conversation_id=conv.id,
                        customer_id=cliente.id,
                        idempotency_key=uniq("ped"),
                        order_seq=seq,
                        public_order_number=f"OC-{seq:04d}",
                        total=Decimal("50000"),
                        status="abierto",
                    )
                )
            await session.flush()
            return cliente.id


async def test_iconos_veraces_y_filtro_por_agente_sin_cruzar_negocios(app_client) -> None:
    tid_a, tid_b = await _negocio(), await _negocio()
    sin_nada = await _cliente_solo(tid_a, con_pedido=False, nombre="Vacío")
    con_pedido = await _cliente_solo(tid_a, con_pedido=True, nombre="Compró")
    del_otro = await _cliente_solo(tid_b, con_pedido=True, nombre="Ajeno")
    headers = await api_headers(app_client, tid_a)

    # Sin datos solo se ve General (antes el resolver de commerce daba «Nuevo» a todos).
    lista = (await app_client.get(URL, headers=headers)).json()
    por_id = {i["id"]: i["agentes"] for i in lista["items"]}
    assert por_id[str(sin_nada)] == ["general"]
    assert por_id[str(con_pedido)] == ["general", "ventas"]

    ventas = (await app_client.get(URL, headers=headers, params={"agente": "ventas"})).json()
    assert {i["id"] for i in ventas["items"]} == {str(con_pedido)}
    assert ventas["total"] == 1
    assert str(del_otro) not in {i["id"] for i in ventas["items"]}

    # General no filtra; las demás áreas sin datos del negocio quedan vacías.
    general = (await app_client.get(URL, headers=headers, params={"agente": "general"})).json()
    assert {str(sin_nada), str(con_pedido)} <= {i["id"] for i in general["items"]}
    agenda = (await app_client.get(URL, headers=headers, params={"agente": "agenda"})).json()
    assert agenda["items"] == [] and agenda["total"] == 0

    # Lo mismo en el CSV, y el filtro se combina con los demás.
    csv_ventas = await app_client.get(f"{URL}/export.csv", headers=headers, params={"agente": "ventas"})
    assert csv_ventas.status_code == 200
    assert "Compró" in csv_ventas.text and "Vacío" not in csv_ventas.text and "Ajeno" not in csv_ventas.text
    nada = await app_client.get(URL, headers=headers, params={"agente": "ventas", "q": "Vacío"})
    assert nada.json()["items"] == []

    # El otro negocio ve solo lo suyo.
    headers_b = await api_headers(app_client, tid_b)
    de_b = (await app_client.get(URL, headers=headers_b, params={"agente": "ventas"})).json()
    assert {i["id"] for i in de_b["items"]} == {str(del_otro)}


async def test_filtro_por_agente_invalido_es_400(app_client) -> None:
    from core.alta_negocio import onboard_tenant

    # Un negocio sin Cobranza: su clave no se puede filtrar aunque exista en el registro.
    slug = uniq("soloventas")
    tid, _ = await onboard_tenant(
        name=f"Solo ventas {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    headers = await api_headers(app_client, tid)
    for clave in ("inventado", "cobranza"):
        for ruta in (URL, f"{URL}/export.csv"):
            r = await app_client.get(ruta, headers=headers, params={"agente": clave})
            assert r.status_code == 400, (ruta, clave, r.text)
            assert r.json()["error"]["code"] == "filtro_invalido"
