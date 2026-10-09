"""HU-SUB-05 · avisos de cobro y mora contra la BD real, con reloj simulado.

Gherkin: link enviado (cargo con anticipación y texto), recordatorios una vez, sin spam,
pausa automática y trazabilidad.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AHORA = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)


async def _negocio(config: dict | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    slug = uniq("avisos")
    tid, _ = await onboard_tenant(name=f"Negocio {slug}", slug=slug, packs=["pack_subscriptions"])
    if config:
        async with tenant_scope(tid):
            await plugin_manager.save_config("pack_subscriptions", config)
    return tid


async def _sub_con_cargo(tid, *, vence: datetime, creado: datetime, estado_cargo="pendiente", **campos):
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(
                tenant_id=tid, customer_id=uuid.uuid4(), concepto="Plan Pro", estado=campos.pop("estado", "activa"),
                valor_periodo=Decimal("200000"), moneda="COP", periodicidad="mensual", dia_ancla=15, **campos,
            )
            s.add(sub)
            await s.flush()
            cargo = SubscriptionCharge(
                tenant_id=tid, subscription_id=sub.id, periodo=vence.strftime("%Y-%m"),
                monto=Decimal("200000"), estado=estado_cargo, fecha_vencimiento=vence,
                link_url="https://pago.test/abc", created_at=creado,
            )
            s.add(cargo)
            await s.flush()
            return sub.id, cargo.id


async def _eventos(tid, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (await s.execute(select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic))).scalars()
            )


@pytest.mark.asyncio
async def test_cargo_con_anticipacion_y_texto_del_negocio() -> None:
    """Gherkin «Link enviado»: el cargo sale `dias_anticipacion` antes, con el texto del pack."""
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio({"aviso_cargo_texto": "Hola {cliente.nombre}, paga aquí: {cargo.link}"})
    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(
                tenant_id=tid, customer_id=uuid.uuid4(), concepto="Plan", estado="activa",
                valor_periodo=Decimal("100000"), periodicidad="mensual", dia_ancla=20,
                proximo_cargo=AHORA + timedelta(days=2),
            )
            s.add(sub)
    await _generar_cargos(AHORA)
    [evento] = await _eventos(tid, "subscription.charge_created")
    assert evento.payload["texto"] == "Hola {cliente.nombre}, paga aquí: {cargo.link}"
    assert evento.payload["fecha_vencimiento"] == (AHORA + timedelta(days=2)).date().isoformat()


@pytest.mark.asyncio
async def test_recordatorio_una_sola_vez() -> None:
    """Gherkin «Recordatorios»."""
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    await _sub_con_cargo(tid, vence=AHORA - timedelta(days=3), creado=AHORA - timedelta(days=6))
    await _avisos_de_cobro(AHORA)
    await _avisos_de_cobro(AHORA)  # el cron corre otra vez el mismo día
    eventos = await _eventos(tid, "subscription.charge_reminder")
    assert [e.payload["dias"] for e in eventos] == [3]
    assert eventos[0].payload["link"] == "https://pago.test/abc"


@pytest.mark.asyncio
async def test_cargo_pagado_no_recibe_recordatorio() -> None:
    """Gherkin «Sin spam»."""
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    await _sub_con_cargo(tid, vence=AHORA - timedelta(days=3), creado=AHORA - timedelta(days=6), estado_cargo="pagado")
    await _avisos_de_cobro(AHORA)
    assert await _eventos(tid, "subscription.charge_reminder") == []


@pytest.mark.asyncio
async def test_pausa_por_mora_y_reactivacion_al_pagar() -> None:
    """Gherkin «Pausa automática»; pagar reactiva la pausada por mora."""
    from pack_subscriptions.jobs import _avisos_de_cobro
    from pack_subscriptions.models import Subscription

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio({"pausar_tras_dias_mora": 10})
    sid, cid = await _sub_con_cargo(
        tid, vence=AHORA - timedelta(days=12), creado=AHORA - timedelta(days=15), estado="vencida"
    )
    await _avisos_de_cobro(AHORA)
    async with tenant_scope(tid):
        async with get_session() as s:
            sub = await s.get(Subscription, sid)
            assert (sub.estado, sub.motivo_pausa) == ("pausada", "mora")
            auditoria = (
                await s.execute(select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == "subs_pausada_por_mora"))
            ).scalars().first()
    assert auditoria is not None
    [pausa] = await _eventos(tid, "subscription.paused")
    assert pausa.payload["dias_mora"] == 12

    plugin_manager.scan()
    async with tenant_scope(tid):
        await plugin_manager.get("pack_subscriptions")._on_payment_confirmed(
            reference=f"sub-{cid}", provider="wompi", provider_reference="t", amount=200000.0
        )
        async with get_session() as s:
            sub = await s.get(Subscription, sid)
    assert (sub.estado, sub.motivo_pausa) == ("activa", None)


@pytest.mark.asyncio
async def test_trazabilidad_de_avisos_del_cargo(app_client) -> None:
    """Gherkin «Trazabilidad»: el cargo muestra qué se envió, cuándo y con qué resultado."""
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    _sid, cid = await _sub_con_cargo(tid, vence=AHORA - timedelta(days=3), creado=AHORA - timedelta(days=6))
    await _avisos_de_cobro(AHORA)
    r = await app_client.get(
        f"/api/v1/pack/pack_subscriptions/cargos/{cid}/avisos", headers=await api_headers(app_client, tid)
    )
    assert r.status_code == 200, r.text
    [aviso] = r.json()
    assert aviso["evento"] == "subscription.charge_reminder"
    assert aviso["estado_evento"] == "pendiente"

    otro = await _negocio()
    r = await app_client.get(
        f"/api/v1/pack/pack_subscriptions/cargos/{cid}/avisos", headers=await api_headers(app_client, otro)
    )
    assert r.status_code == 404
