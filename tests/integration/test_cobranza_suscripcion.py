"""Suscripción recurrente por cliente (ADR-0082): cron + tools. Requiere stack local."""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _tenant_cobranza() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = uniq("sub")
    tenant_id, _ = await onboard_tenant(
        name=f"Suscripción {slug}",
        slug=slug,
        business_description="Cartera recurrente de prueba.",
        persona={"tone": "cercano"},
        packs=["pack_cobranza"],
    )
    return tenant_id


async def test_cron_genera_cargo_y_avanza_periodo() -> None:
    from pack_cobranza.jobs import generar_cargos_suscripcion
    from pack_cobranza.models import Cartera, Cliente, Cuota, Suscripcion
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_cobranza()
    hoy = date.today()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cartera = Cartera(tenant_id=tenant_id, nombre="Cartera X")
            session.add(cartera)
            await session.flush()
            cliente = Cliente(tenant_id=tenant_id, cartera_id=cartera.id, nombre="Ana")
            session.add(cliente)
            await session.flush()
            sub = Suscripcion(
                tenant_id=tenant_id,
                cliente_id=cliente.id,
                cartera_id=cartera.id,
                concepto="Plan Basic",
                valor_periodo=200000,
                dia_facturacion=15,
                estado="activa",
                proximo_cargo=hoy,
            )
            session.add(sub)
            await session.flush()
            sub_id = sub.id

    await generar_cargos_suscripcion()

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cuotas = (
                await session.execute(select(Cuota).where(Cuota.suscripcion_id == sub_id))
            ).scalars().all()
            assert len(cuotas) == 1
            assert float(cuotas[0].valor_esperado) == 200000
            assert cuotas[0].prestamo_id is None
            sub = await session.get(Suscripcion, sub_id)
            assert sub is not None and sub.proximo_cargo is not None and sub.proximo_cargo > hoy


async def test_tools_crear_y_consultar_suscripcion() -> None:
    from pack_cobranza.models import Cartera, Cliente
    from pack_cobranza.skills import CobranzaSkill

    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_cobranza()
    customer_id = uuid.uuid4()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cartera = Cartera(tenant_id=tenant_id, nombre="Cartera Y")
            session.add(cartera)
            await session.flush()
            session.add(
                Cliente(
                    tenant_id=tenant_id,
                    cartera_id=cartera.id,
                    customer_id=customer_id,
                    nombre="Beto",
                )
            )

    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=str(uuid.uuid4()),
        channel="test",
    )
    skill = CobranzaSkill()
    async with tenant_scope(tenant_id):
        creada = await skill.crear_suscripcion(
            {"concepto": "Plan Basic", "valor": 200000, "dia_facturacion": 10}, ctx
        )
        assert creada.get("creada") is True
        consulta = await skill.consultar_suscripcion({}, ctx)
        assert consulta["encontrado"] is True
        assert consulta["concepto"] == "Plan Basic"
        assert consulta["estado"] == "activa"
        # Idempotente: no crea una segunda suscripción activa.
        otra = await skill.crear_suscripcion({"concepto": "Otra", "valor": 1}, ctx)
        assert otra.get("ya_existe") is True
        # Pausar / renovar.
        pausada = await skill.pausar_suscripcion({"motivo": "viaje"}, ctx)
        assert pausada["estado"] == "pausada"
        renovada = await skill.renovar_suscripcion({}, ctx)
        assert renovada["estado"] == "activa"


# ---------------------------------------------------------------- HU-SUB-02
async def _cuota_suscripcion_con_link(tenant_id, *, valor: float):
    """Crea una cuota de suscripción (sin préstamo) y su link, listos para el webhook."""
    from datetime import date

    from pack_cobranza.models import Cartera, Cliente, Cuota, LinkPago, Suscripcion

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cartera = Cartera(tenant_id=tenant_id, nombre=f"Cartera {uniq('c')}")
            session.add(cartera)
            await session.flush()
            cliente = Cliente(tenant_id=tenant_id, cartera_id=cartera.id, nombre="Ana")
            session.add(cliente)
            await session.flush()
            sub = Suscripcion(
                tenant_id=tenant_id, cliente_id=cliente.id, cartera_id=cartera.id,
                concepto="Plan", valor_periodo=valor, dia_facturacion=1, estado="activa",
            )
            session.add(sub)
            await session.flush()
            cuota = Cuota(
                tenant_id=tenant_id, prestamo_id=None, suscripcion_id=sub.id,
                numero_cuota=1, valor_esperado=valor, fecha_vencimiento=date.today(),
                estatus="pendiente",
            )
            session.add(cuota)
            await session.flush()
            link = LinkPago(
                tenant_id=tenant_id, cuota_id=cuota.id, prestamo_id=None,
                cliente_id=cliente.id, referencia=f"cob-{cuota.id}", url="https://pay/x",
                monto=valor, estado="activo",
            )
            session.add(link)
            await session.flush()
            return tenant_id, cuota.id, cliente.id, link.referencia


async def _pagar(tenant_id, referencia: str, monto: float) -> None:
    from core.shared.events import ON_PAYMENT_CONFIRMED, event_bus
    from core.shared.tenant import tenant_scope

    # El handler está suscrito globalmente; se emite dentro del tenant.
    async with tenant_scope(tenant_id):
        await event_bus.emit(
            ON_PAYMENT_CONFIRMED, reference=referencia, provider="wompi",
            provider_reference="ref", amount=monto,
        )


async def test_pago_en_linea_de_cuota_de_suscripcion_registra_pago() -> None:
    """Pago completo: crea Pago (prestamo_id NULL), cuota pagada y link usado."""
    from pack_cobranza.models import Cuota, LinkPago, Pago
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_cobranza()
    _tid, cuota_id, _cliente_id, ref = await _cuota_suscripcion_con_link(tenant_id, valor=200000)

    await _pagar(tenant_id, ref, 200000)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            pagos = (
                await session.execute(select(Pago).where(Pago.cuota_id == cuota_id))
            ).scalars().all()
            assert len(pagos) == 1, "el pago debe registrarse"
            assert pagos[0].prestamo_id is None
            assert float(pagos[0].valor) == 200000
            cuota = await session.get(Cuota, cuota_id)
            assert cuota.estatus == "pagada"
            link = (await session.execute(select(LinkPago).where(LinkPago.referencia == ref))).scalar_one()
            assert link.estado == "usado"


async def test_pago_parcial_deja_la_cuota_pendiente() -> None:
    """Un monto menor a la cuota se registra como abono y la cuota sigue pendiente."""
    from pack_cobranza.models import Cuota, LinkPago, Pago
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_cobranza()
    _tid, cuota_id, _cliente_id, ref = await _cuota_suscripcion_con_link(tenant_id, valor=200000)

    await _pagar(tenant_id, ref, 50000)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            assert (await session.execute(select(Pago).where(Pago.cuota_id == cuota_id))).scalars().all()
            cuota = await session.get(Cuota, cuota_id)
            assert cuota.estatus == "pendiente", "un abono parcial no salda la cuota"
            link = (await session.execute(select(LinkPago).where(LinkPago.referencia == ref))).scalar_one()
            # El link se marca usado (el pago ocurrió); el abono queda registrado.
            assert link.estado == "usado"


async def test_webhook_duplicado_no_registra_dos_pagos() -> None:
    """Reenviar el webhook con la misma referencia no duplica el pago."""
    from pack_cobranza.models import Pago
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_cobranza()
    _tid, cuota_id, _cliente_id, ref = await _cuota_suscripcion_con_link(tenant_id, valor=200000)

    await _pagar(tenant_id, ref, 200000)
    await _pagar(tenant_id, ref, 200000)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            pagos = (
                await session.execute(select(Pago).where(Pago.cuota_id == cuota_id))
            ).scalars().all()
            assert len(pagos) == 1, "el link usado no debe generar un segundo pago"


async def test_marcar_mora_tolera_cuotas_sin_prestamo() -> None:
    """El cron de mora corre con cuotas de suscripción (sin préstamo)."""
    from datetime import date, timedelta

    from pack_cobranza.jobs import marcar_mora
    from pack_cobranza.models import Cartera, Cliente, Cuota, Suscripcion

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_cobranza()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cartera = Cartera(tenant_id=tenant_id, nombre=f"Cartera {uniq('m')}")
            session.add(cartera)
            await session.flush()
            cliente = Cliente(tenant_id=tenant_id, cartera_id=cartera.id, nombre="Ana")
            session.add(cliente)
            await session.flush()
            sub = Suscripcion(
                tenant_id=tenant_id, cliente_id=cliente.id, cartera_id=cartera.id,
                concepto="Plan", valor_periodo=100000, dia_facturacion=1, estado="activa",
            )
            session.add(sub)
            await session.flush()
            cuota = Cuota(
                tenant_id=tenant_id, prestamo_id=None, suscripcion_id=sub.id,
                numero_cuota=1, valor_esperado=100000,
                fecha_vencimiento=date.today() - timedelta(days=3), estatus="pendiente",
            )
            session.add(cuota)
            await session.flush()
            cuota_id = cuota.id

    await marcar_mora()

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cuota = await session.get(Cuota, cuota_id)
            assert cuota.estatus == "atrasada"
