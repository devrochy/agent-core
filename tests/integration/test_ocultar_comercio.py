"""HU-AOS-71 · ocultar el comercio de un negocio (borrado lógico de productos, pedidos y cotizaciones).

- **Ocultar con borrado lógico:** lo oculto no aparece en la ficha, el timeline, las columnas de Clientes, la
  búsqueda ni las tools del agente; sigue en la base; la APK lo recibe en `deletedIds`.
- **Idempotente** y **no reaparece** al reencender comercio.
- **Aislamiento:** el otro negocio con comercio no cambia.
- **Orden seguro:** sin un lead del formulario en el intake nuevo o con comercio encendido, el comando no oculta.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio_con_comercio() -> dict[str, uuid.UUID]:
    """Negocio con comercio y un producto, un pedido y una cotización de un cliente."""
    from _habilidades import onboard_tenant

    slug = f"aos71o-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS71 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)
    cliente, conv = uuid.uuid4(), uuid.uuid4()
    producto, pedido, cotizacion = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seq = abs(hash(uuid.uuid4())) % 1_000_000
    await _sql(
        tid,
        "insert into pack_commerce_products (id, tenant_id, name, description) values (:id, :t, 'Plan viejo', '')",
        id=producto, t=tid,
    )
    await _sql(
        tid,
        "insert into pack_commerce_orders (id, tenant_id, conversation_id, customer_id, idempotency_key, "
        "order_seq, public_order_number, status, payment_status, total) "
        "values (:id, :t, :c, :cli, :k, :s, :n, 'abierto', 'pendiente', 100)",
        id=pedido, t=tid, c=conv, cli=cliente, k=uuid.uuid4().hex, s=seq, n=f"P-{seq:04d}",
    )
    await _sql(
        tid,
        "insert into pack_commerce_quotes (id, tenant_id, conversation_id, customer_id, subtotal, discount, total, "
        "status) values (:id, :t, :c, :cli, 50, 0, 50, 'draft')",
        id=cotizacion, t=tid, c=conv, cli=cliente,
    )
    return {"tid": tid, "cliente": cliente, "conv": conv, "producto": producto, "pedido": pedido, "cotizacion": cotizacion}


async def _ocultos(tid: uuid.UUID) -> dict[str, int]:
    filas = await _sql(
        tid,
        "select 'productos', count(*) filter (where deleted_at is not null), count(*) from pack_commerce_products "
        "where tenant_id = :t union all "
        "select 'pedidos', count(*) filter (where deleted_at is not null), count(*) from pack_commerce_orders "
        "where tenant_id = :t union all "
        "select 'cotizaciones', count(*) filter (where deleted_at is not null), count(*) from pack_commerce_quotes "
        "where tenant_id = :t",
        t=tid,
    )
    return {nombre: (ocultos, total) for nombre, ocultos, total in filas}


async def _lo_que_se_ve(n: dict[str, uuid.UUID]) -> dict[str, Any]:
    """Las lecturas del panel (ficha, columnas, timeline, búsqueda), de la APK (sync) y del agente."""
    from pack_commerce import ficha, providers
    from pack_commerce import sync as _sync  # noqa: F401  (registra el adapter de pedidos)
    from pack_commerce.search import _buscar_comercio
    from pack_commerce.skills import _order_del_cliente, _quote_del_cliente

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.sync import ejecutar_sync, sync_adapters

    tid = n["tid"]
    async with tenant_scope(tid):
        columnas = (await providers._resolver([n["cliente"]]))[n["cliente"]]
        detalle = await ficha.detalle(n["cliente"], 10)
        timeline = await providers._timeline(n["cliente"], None, 10)
        busqueda = await _buscar_comercio(tid, "P-", 10)
        async with get_session() as session:
            pedido = await _order_del_cliente(session, n["pedido"], n["cliente"])
            cotizacion = await _quote_del_cliente(session, n["cotizacion"], n["cliente"])
        _, _, borrados = await ejecutar_sync(sync_adapters()["pedidos"], tid, datetime(2000, 1, 1, tzinfo=UTC), 100)
    return {
        "pedidos": columnas["pedidos"],
        "cotizaciones_abiertas": columnas["cotizaciones_abiertas"],
        "detalle": len(detalle),
        "timeline": len(timeline),
        "busqueda": len(busqueda),
        "tool_pedido": pedido is not None,
        "tool_cotizacion": cotizacion is not None,
        "sync_borrados": borrados,
    }


async def _poner_comercio(tid: uuid.UUID, encendido: bool) -> None:
    from _habilidades import dar_habilidades, quitar_habilidades

    if encendido:
        await dar_habilidades(tid, ["pack_commerce"])
    else:
        await quitar_habilidades(tid, ["pack_commerce"])


async def test_ocultar_no_se_ve_sigue_en_la_base_no_reaparece_y_no_toca_otro_negocio() -> None:
    from pack_commerce.ocultar import ocultar_comercio

    from core.shared.tenant import tenant_scope

    a, b = await _negocio_con_comercio(), await _negocio_con_comercio()
    antes = await _lo_que_se_ve(a)
    assert antes["pedidos"] == 1 and antes["cotizaciones_abiertas"] == 1 and antes["tool_pedido"]

    await _poner_comercio(a["tid"], False)
    async with tenant_scope(a["tid"]):
        ocultos = await ocultar_comercio(a["tid"])
    assert (ocultos.productos, ocultos.pedidos, ocultos.cotizaciones) == (1, 1, 1)

    despues = await _lo_que_se_ve(a)
    assert despues == {
        "pedidos": 0,
        "cotizaciones_abiertas": 0,
        "detalle": 0,
        "timeline": 0,
        "busqueda": 0,
        "tool_pedido": False,
        "tool_cotizacion": False,
        "sync_borrados": [str(a["pedido"])],
    }
    # Siguen en la base, solo marcados.
    assert await _ocultos(a["tid"]) == {"productos": (1, 1), "pedidos": (1, 1), "cotizaciones": (1, 1)}

    # Idempotente.
    async with tenant_scope(a["tid"]):
        assert (await ocultar_comercio(a["tid"])).total == 0

    # Reencender comercio no los devuelve.
    await _poner_comercio(a["tid"], True)
    assert (await _lo_que_se_ve(a))["pedidos"] == 0
    assert await _ocultos(a["tid"]) == {"productos": (1, 1), "pedidos": (1, 1), "cotizaciones": (1, 1)}

    # El otro negocio no cambia.
    assert await _ocultos(b["tid"]) == {"productos": (0, 1), "pedidos": (0, 1), "cotizaciones": (0, 1)}
    assert (await _lo_que_se_ve(b))["pedidos"] == 1


async def test_orden_seguro_exige_lead_del_formulario_y_comercio_apagado() -> None:
    from cli.commands.tenant import _orden_seguro_para_ocultar
    from core.shared.tenant import tenant_scope

    n = await _negocio_con_comercio()
    tid = n["tid"]

    async def _faltas(desde: datetime | None = None) -> list[str]:
        async with tenant_scope(tid):
            return (await _orden_seguro_para_ocultar(tid, desde))[0]

    faltas = await _faltas()
    assert len(faltas) == 2 and "lead" in faltas[0] and "comercio" in faltas[1]

    # Un lead copiado de commerce (origen NULL) no cuenta como prueba del intake nuevo.
    insertar = (
        "insert into leads (id, tenant_id, customer_id, name, interest, score, status, origen, last_activity_at, "
        "created_at) values (:id, :t, :c, 'Ana', 'planes', 0, 'nuevo', :o, now(), :cuando)"
    )
    await _sql(tid, insertar, id=uuid.uuid4(), t=tid, c=uuid.uuid4(), o=None, cuando=datetime.now(UTC))
    assert any("lead" in f for f in await _faltas())

    hace_un_rato = datetime.now(UTC) - timedelta(hours=1)
    await _sql(tid, insertar, id=uuid.uuid4(), t=tid, c=uuid.uuid4(), o="landing", cuando=hace_un_rato)
    assert await _faltas() == [next(f for f in faltas if "comercio" in f)]
    # Con `--desde` posterior al lead, ese lead no alcanza.
    assert any("lead" in f for f in await _faltas(datetime.now(UTC)))

    await _poner_comercio(tid, False)
    assert await _faltas() == []
    # Sin ocultar todavía: la guarda no escribe.
    assert await _ocultos(tid) == {"productos": (0, 1), "pedidos": (0, 1), "cotizaciones": (0, 1)}
