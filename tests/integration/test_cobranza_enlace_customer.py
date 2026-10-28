"""Enlace Cliente de cobranza ↔ Customer del chat por teléfono (HU-CRM-15, ADR-0179 §7).

Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"


async def _tenant(slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba del enlace de cobranza.",
        admin_email=f"{slug}@test.local",
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    return tid


async def _sembrar(tid: uuid.UUID, *, telefonos: list[str | None], wa: str | None = None):
    """Una cartera con un Cliente por teléfono y, si se pide, un Customer de WhatsApp con `wa`."""
    from pack_cobranza.models import Cartera, Cliente

    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        cartera = Cartera(tenant_id=tid, nombre="Cartera A")
        s.add(cartera)
        await s.flush()
        clientes = [
            Cliente(tenant_id=tid, cartera_id=cartera.id, nombre=f"Deudor {i}", telefono_whatsapp=t)
            for i, t in enumerate(telefonos)
        ]
        s.add_all(clientes)
        customer = None
        if wa is not None:
            customer = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{wa}", contact_phone=wa, name="Chat"
            )
            s.add(customer)
        await s.flush()
        return cartera.id, [c.id for c in clientes], customer.id if customer else None


async def _customer_ids(tid: uuid.UUID) -> dict[uuid.UUID, uuid.UUID | None]:
    from pack_cobranza.models import Cliente
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        filas = (await s.execute(select(Cliente.id, Cliente.customer_id).where(Cliente.tenant_id == tid))).all()
    return dict(filas)


async def test_lazy_enlaza_por_telefono_local_contra_wa_id() -> None:
    from pack_cobranza.skills import _cliente_de_customer

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant(f"enl-lazy-{uuid.uuid4().hex[:6]}")
    _, (cid,), customer = await _sembrar(tid, telefonos=["(300) 123-4567"], wa="573001234567")

    async with tenant_scope(tid), get_session() as s:
        cliente = await _cliente_de_customer(s, customer)
    assert cliente is not None and cliente.id == cid
    assert (await _customer_ids(tid))[cid] == customer


async def test_otro_negocio_con_el_mismo_telefono_no_se_enlaza() -> None:
    from pack_cobranza.enlazar import enlazar_cliente, enlazar_customer

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a = await _tenant(f"enl-a-{uuid.uuid4().hex[:6]}")
    b = await _tenant(f"enl-b-{uuid.uuid4().hex[:6]}")
    _, (cli_a,), customer_a = await _sembrar(a, telefonos=[None], wa="573009990000")
    _, (cli_b,), customer_b = await _sembrar(b, telefonos=["3009990000"], wa=None)

    # El Customer de A no ve al Cliente de B…
    async with tenant_scope(a), get_session() as s:
        assert (await enlazar_customer(s, customer_a)).value == "sin_match"
    # …ni el Cliente de B al Customer de A.
    from pack_cobranza.models import Cliente

    async with tenant_scope(b), get_session() as s:
        cliente = await s.get(Cliente, cli_b)
        assert (await enlazar_cliente(s, cliente)).value == "sin_match"
    assert (await _customer_ids(b))[cli_b] is None
    assert (await _customer_ids(a))[cli_a] is None
    assert customer_b is None


async def test_dos_deudores_con_el_mismo_telefono_no_se_enlazan() -> None:
    from pack_cobranza.enlazar import enlazar_customer

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant(f"enl-amb-{uuid.uuid4().hex[:6]}")
    _, ids, customer = await _sembrar(tid, telefonos=["3001112222", "+57 300 111 2222"], wa="573001112222")

    async with tenant_scope(tid), get_session() as s:
        assert (await enlazar_customer(s, customer)).value == "ambiguo"
    assert set((await _customer_ids(tid)).values()) == {None}
    assert len(ids) == 2


async def test_dos_enlaces_concurrentes_producen_uno() -> None:
    from pack_cobranza.enlazar import Resultado, enlazar_customer

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant(f"enl-conc-{uuid.uuid4().hex[:6]}")
    _, (cid,), customer = await _sembrar(tid, telefonos=["3004445555"], wa="573004445555")

    async def intento() -> Resultado:
        async with tenant_scope(tid), get_session() as s:
            return await enlazar_customer(s, customer)

    resultados = await asyncio.gather(intento(), intento())
    assert [r for r in resultados if r is Resultado.ENLAZADO] == [Resultado.ENLAZADO]
    assert (await _customer_ids(tid))[cid] == customer


async def test_customer_ya_enlazado_a_otro_cliente_no_se_reasigna() -> None:
    from pack_cobranza.enlazar import enlazar_cliente
    from pack_cobranza.models import Cliente

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant(f"enl-ya-{uuid.uuid4().hex[:6]}")
    _, (primero, segundo), customer = await _sembrar(tid, telefonos=["3006667777", None], wa="573006667777")
    async with tenant_scope(tid), get_session() as s:
        (await s.get(Cliente, primero)).customer_id = customer
    # El segundo se carga después con el mismo teléfono: no le quita el Customer al primero.
    async with tenant_scope(tid), get_session() as s:
        otro = await s.get(Cliente, segundo)
        otro.telefono_whatsapp = "300 666 7777"
        assert (await enlazar_cliente(s, otro)).value == "ya_enlazado"
    ids = await _customer_ids(tid)
    assert ids[primero] == customer and ids[segundo] is None


async def test_backfill_dry_run_no_escribe_y_luego_enlaza() -> None:
    from pack_cobranza.enlazar import enlazar_existentes

    tid = await _tenant(f"enl-cli-{uuid.uuid4().hex[:6]}")
    _, (con, sin_match), customer = await _sembrar(tid, telefonos=["3008889999", "3100000000"], wa="573008889999")

    seco = await enlazar_existentes(dry_run=True, tenant_id=tid)
    assert (seco.resultados["enlazado"], seco.resultados["sin_match"]) == (1, 1)
    assert set((await _customer_ids(tid)).values()) == {None}

    real = await enlazar_existentes(dry_run=False, tenant_id=tid, lote=1)
    assert (real.resultados["enlazado"], real.resultados["sin_match"]) == (1, 1)
    ids = await _customer_ids(tid)
    assert ids[con] == customer and ids[sin_match] is None

    # Idempotente: ya no queda nada por enlazar con teléfono que cruce.
    otra = await enlazar_existentes(dry_run=False, tenant_id=tid)
    assert otra.resultados["enlazado"] == 0


async def test_alta_por_el_panel_normaliza_y_enlaza(app_client) -> None:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"enl-api-{uuid.uuid4().hex[:6]}"
    tid = await _tenant(slug)
    cartera_id, _, customer = await _sembrar(tid, telefonos=[], wa="573002223333")
    r = await app_client.post("/api/v1/auth/login", json={"identifier": f"{slug}@test.local", "password": PW})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}", "X-Tenant-Id": str(tid)}

    r = await app_client.post(
        "/api/v1/pack/cobranza/clients",
        headers=h,
        json={"nombre": "Nueva", "telefono": "300 222 3333", "cartera_id": str(cartera_id)},
    )
    assert r.status_code == 201, r.text
    assert r.json()["telefono"] == "+573002223333"
    ids = await _customer_ids(tid)
    assert list(ids.values()) == [customer]
    async with tenant_scope(tid), get_session() as s:
        assert await s.get(Customer, customer) is not None


async def test_cache_negativa_y_redis_caido_no_rompen_la_tool(monkeypatch) -> None:
    from pack_cobranza import enlazar
    from pack_cobranza.skills import _cliente_de_customer

    from core.db.session import get_session
    from core.memory.working import get_redis
    from core.shared.tenant import tenant_scope

    tid = await _tenant(f"enl-neg-{uuid.uuid4().hex[:6]}")
    _, _, customer = await _sembrar(tid, telefonos=[], wa="573005556666")
    clave = f"tenant:{tid}:cobr_enlace_neg:{customer}"

    async with tenant_scope(tid), get_session() as s:
        assert await _cliente_de_customer(s, customer) is None
    assert 0 < await get_redis().ttl(clave) <= enlazar.TTL_CACHE_NEGATIVO_S

    # Con Redis caído la tool sigue: intenta el enlace igual (aquí hay un Cliente nuevo que cruza).
    await get_redis().delete(clave)
    _, (cid,), _ = await _sembrar(tid, telefonos=["3005556666"])

    def roto():
        raise ConnectionError("redis caído")

    monkeypatch.setattr("core.memory.working.get_redis", roto)
    async with tenant_scope(tid), get_session() as s:
        cliente = await _cliente_de_customer(s, customer)
    assert cliente is not None and cliente.id == cid
