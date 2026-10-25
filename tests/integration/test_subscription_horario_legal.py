"""HU-SUB-18 · avisos de cobro en horario legal, contra la BD real con reloj simulado.

Gherkin: domingo o festivo (se difiere al siguiente día hábil), zona horaria del negocio,
uno por día, y lo que no es cobro no se restringe (confirmación de pago: no pasa por aquí).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# martes 27 de octubre de 2026, 22:15 en Bogotá: la hora del cron de cargos.
NOCHE = datetime(2026, 10, 28, 3, 15, tzinfo=UTC)


async def _negocio(tz: str | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("legal")
    tid, _ = await onboard_tenant(name=f"Negocio {slug}", slug=slug, packs=["pack_subscriptions"])
    if tz:
        async with tenant_scope(tid):
            async with get_session() as s:
                fila = (await s.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalars().first()
                fila.timezone = tz
    return tid


async def _sub(tid, *, cliente: uuid.UUID, proximo: datetime):
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(
                tenant_id=tid, customer_id=cliente, concepto="Plan", estado="activa",
                valor_periodo=Decimal("100000"), periodicidad="mensual", dia_ancla=proximo.day,
                proximo_cargo=proximo,
            )
            s.add(sub)
            await s.flush()
            return sub.id


async def _cargo(tid, *, cliente: uuid.UUID, vence: datetime, creado: datetime):
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(
                tenant_id=tid, customer_id=cliente, concepto="Plan", estado="activa",
                valor_periodo=Decimal("100000"), periodicidad="mensual", dia_ancla=vence.day,
            )
            s.add(sub)
            await s.flush()
            cargo = SubscriptionCharge(
                tenant_id=tid, subscription_id=sub.id, periodo=vence.strftime("%Y-%m"),
                monto=Decimal("100000"), estado="pendiente", fecha_vencimiento=vence,
                link_url="https://pago.test/x", created_at=creado,
            )
            s.add(cargo)
            await s.flush()
            return cargo.id


async def _eventos(tid, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(
                        select(DomainEvent)
                        .where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic)
                        .order_by(DomainEvent.next_attempt_at)
                    )
                ).scalars()
            )


@pytest.mark.asyncio
async def test_aviso_de_cargo_generado_de_noche_espera_a_las_siete() -> None:
    from pack_subscriptions.jobs import _generar_cargos

    tid = await _negocio()
    await _sub(tid, cliente=uuid.uuid4(), proximo=NOCHE + timedelta(days=2))
    await _generar_cargos(NOCHE)
    [evento] = await _eventos(tid, "subscription.charge_created")
    assert evento.next_attempt_at == datetime(2026, 10, 28, 12, 0, tzinfo=UTC)  # miércoles 07:00 Bogotá


@pytest.mark.asyncio
async def test_sabado_en_la_tarde_salta_domingo_y_lunes_festivo() -> None:
    from pack_subscriptions.jobs import _generar_cargos

    tid = await _negocio()
    sabado_tarde = datetime(2026, 10, 31, 21, 0, tzinfo=UTC)  # 16:00 Bogotá
    await _sub(tid, cliente=uuid.uuid4(), proximo=sabado_tarde + timedelta(days=3))
    await _generar_cargos(sabado_tarde)
    [evento] = await _eventos(tid, "subscription.charge_created")
    assert evento.next_attempt_at == datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # martes 3, 07:00


@pytest.mark.asyncio
async def test_dos_cargos_del_mismo_cliente_salen_en_dias_distintos() -> None:
    from pack_subscriptions.jobs import _generar_cargos

    tid = await _negocio()
    cliente = uuid.uuid4()
    await _sub(tid, cliente=cliente, proximo=NOCHE + timedelta(days=2))
    await _sub(tid, cliente=cliente, proximo=NOCHE + timedelta(days=2))
    await _generar_cargos(NOCHE)
    eventos = await _eventos(tid, "subscription.charge_created")
    assert [e.next_attempt_at for e in eventos] == [
        datetime(2026, 10, 28, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 29, 12, 0, tzinfo=UTC),
    ]


@pytest.mark.asyncio
async def test_la_ventana_se_evalua_en_la_zona_del_negocio() -> None:
    from pack_subscriptions.jobs import _generar_cargos

    tid = await _negocio("America/Mexico_City")  # UTC-6
    await _sub(tid, cliente=uuid.uuid4(), proximo=NOCHE + timedelta(days=2))
    await _generar_cargos(NOCHE)
    [evento] = await _eventos(tid, "subscription.charge_created")
    assert evento.next_attempt_at == datetime(2026, 10, 28, 13, 0, tzinfo=UTC)  # 07:00 en CDMX


@pytest.mark.asyncio
async def test_recordatorios_no_salen_en_domingo_y_salen_el_siguiente_dia_habil() -> None:
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    domingo = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)  # domingo 10:00 Bogotá
    await _cargo(tid, cliente=uuid.uuid4(), vence=domingo - timedelta(days=3), creado=domingo - timedelta(days=6))
    totales = await _avisos_de_cobro(domingo)
    assert totales["fuera_de_horario"] >= 1
    assert await _eventos(tid, "subscription.charge_reminder") == []
    await _avisos_de_cobro(domingo + timedelta(days=1))  # lunes 9 de nov, 10:00
    [recordatorio] = await _eventos(tid, "subscription.charge_reminder")
    assert recordatorio.payload["dias"] == 3


@pytest.mark.asyncio
async def test_un_solo_aviso_de_cobro_por_cliente_y_dia() -> None:
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    martes = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # martes 10:00 Bogotá
    cliente = uuid.uuid4()
    await _cargo(tid, cliente=cliente, vence=martes - timedelta(days=3), creado=martes - timedelta(days=6))
    await _cargo(tid, cliente=cliente, vence=martes - timedelta(days=3), creado=martes - timedelta(days=6))
    await _avisos_de_cobro(martes)
    await _avisos_de_cobro(martes + timedelta(hours=2))  # el cron corre cada hora
    assert len(await _eventos(tid, "subscription.charge_reminder")) == 1
    await _avisos_de_cobro(martes + timedelta(days=1))
    assert len(await _eventos(tid, "subscription.charge_reminder")) == 2
