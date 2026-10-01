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
