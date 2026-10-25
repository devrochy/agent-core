"""HU-SUB-04 · ciclo de vida de la suscripción contra la BD real, con reloj simulado.

Gherkin: período real (en el cron), vencida, reactivación, monto validado, sin cobros
futuros ni en prueba, config usada y RLS forzada.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq
from sqlalchemy import select, text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AHORA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("subs")
    tid, _ = await onboard_tenant(name=f"Negocio {slug}", slug=slug, packs=["pack_subscriptions"])
    return tid


async def _sub(tid, **campos):
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    valores = {
        "customer_id": uuid.uuid4(), "concepto": "Plan Pro", "estado": "activa",
        "valor_periodo": Decimal("200000"), "moneda": "COP", "periodicidad": "mensual",
        "dia_ancla": 15, "fecha_alta": datetime(2026, 10, 15, 9, tzinfo=UTC),
        **campos,
    }
    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(tenant_id=tid, **valores)
            s.add(sub)
            await s.flush()
            return sub.id


async def _cargo(tid, sub_id, *, vence: datetime, monto="200000", periodo="2026-11"):
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            c = SubscriptionCharge(
                tenant_id=tid, subscription_id=sub_id, periodo=periodo,
                monto=Decimal(monto), estado="pendiente", fecha_vencimiento=vence,
            )
            s.add(c)
            await s.flush()
            return c.id


async def _leer(tid, modelo, id_):
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await s.get(modelo, id_)


@pytest.mark.asyncio
async def test_cron_usa_el_calendario_y_no_toca_el_estado() -> None:
    """Gherkin «Período real» en el cron: anual → +1 año; ya no pone `activa` sin pago."""
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import Subscription

    tid = await _negocio()
    anual = await _sub(
        tid, periodicidad="anual", estado="trialing",
        proximo_cargo=datetime(2026, 10, 15, 9, tzinfo=UTC), trial_termina=datetime(2026, 10, 15, 9, tzinfo=UTC),
    )
    await _generar_cargos(AHORA)
    sub = await _leer(tid, Subscription, anual)
    assert sub.proximo_cargo == datetime(2027, 10, 15, 9, tzinfo=UTC)
    assert sub.estado == "trialing"  # antes el cron ponía `activa` aunque no pagara


@pytest.mark.asyncio
async def test_no_cobra_durante_la_prueba() -> None:
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    sid = await _sub(
        tid, estado="trialing", proximo_cargo=AHORA - timedelta(days=1), trial_termina=AHORA + timedelta(days=5)
    )
    await _generar_cargos(AHORA)
    async with tenant_scope(tid):
        async with get_session() as s:
            cargos = (await s.execute(select(SubscriptionCharge).where(SubscriptionCharge.subscription_id == sid))).scalars().all()
    assert cargos == []


@pytest.mark.asyncio
async def test_vencida_tras_los_dias_de_gracia_y_emite_past_due() -> None:
    """Gherkin «Vencida» (gracia por defecto: 5 días)."""
    from pack_subscriptions.jobs import _marcar_vencidas
    from pack_subscriptions.models import Subscription

    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    vencida = await _sub(tid)
    cargo_viejo = await _cargo(tid, vencida, vence=AHORA - timedelta(days=6))
    al_dia = await _sub(tid)
    await _cargo(tid, al_dia, vence=AHORA - timedelta(days=4))

    await _marcar_vencidas(AHORA)
    assert (await _leer(tid, Subscription, vencida)).estado == "vencida"
    assert (await _leer(tid, Subscription, al_dia)).estado == "activa"
    async with tenant_scope(tid):
        async with get_session() as s:
            evento = (
                await s.execute(select(DomainEvent).where(DomainEvent.dedupe_key == f"sub_past_due:{cargo_viejo}"))
            ).scalar_one()
    assert evento.topic == "subscription.past_due"


@pytest.mark.asyncio
async def test_config_de_dias_de_gracia_se_respeta() -> None:
    """Gherkin «Config usada»: con 10 días de gracia, 6 días de atraso no la vencen."""
    from pack_subscriptions.jobs import _marcar_vencidas
    from pack_subscriptions.models import Subscription

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_subscriptions", {"dias_gracia": 10})
    sid = await _sub(tid)
    await _cargo(tid, sid, vence=AHORA - timedelta(days=6))
    await _marcar_vencidas(AHORA)
    assert (await _leer(tid, Subscription, sid)).estado == "activa"


async def _pagar(tid, cargo_id, monto: float) -> None:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()
    modulo = plugin_manager.get("pack_subscriptions")
    async with tenant_scope(tid):
        await modulo._on_payment_confirmed(
            reference=f"sub-{cargo_id}", provider="wompi", provider_reference="test_x", amount=monto
        )


@pytest.mark.asyncio
async def test_reactivacion_al_pagar_desde_el_periodo_pagado() -> None:
    """Gherkin «Reactivación»."""
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    tid = await _negocio()
    sid = await _sub(tid, estado="vencida", proximo_cargo=datetime(2026, 11, 15, 9, tzinfo=UTC))
    cid = await _cargo(tid, sid, vence=datetime(2026, 11, 15, 9, tzinfo=UTC))
    await _pagar(tid, cid, 200000.0)
    sub = await _leer(tid, Subscription, sid)
    assert sub.estado == "activa"
    assert sub.proximo_cargo == datetime(2026, 12, 15, 9, tzinfo=UTC)
    assert (await _leer(tid, SubscriptionCharge, cid)).estado == "pagado"


@pytest.mark.asyncio
async def test_monto_distinto_no_marca_pagado_y_queda_auditado() -> None:
    """Gherkin «Monto validado»."""
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    sid = await _sub(tid, estado="vencida")
    cid = await _cargo(tid, sid, vence=AHORA - timedelta(days=10))
    await _pagar(tid, cid, 1000.0)
    assert (await _leer(tid, SubscriptionCharge, cid)).estado == "pendiente"
    assert (await _leer(tid, Subscription, sid)).estado == "vencida"
    async with tenant_scope(tid):
        async with get_session() as s:
            alerta = (
                await s.execute(select(AuditLog).where(AuditLog.action == "subs_pago_monto_distinto", AuditLog.tenant_id == tid))
            ).scalars().first()
    assert alerta is not None


@pytest.mark.asyncio
async def test_link_sin_periodos_futuros_ni_en_prueba() -> None:
    """Gherkin «Sin cobros futuros»."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    tid = await _negocio()
    en_prueba = await _sub(tid, estado="trialing", trial_termina=datetime.now(UTC) + timedelta(days=10))
    activa = await _sub(tid, periodo_actual_inicio=datetime(2026, 10, 15, 9, tzinfo=UTC))
    cap = SubscriptionCapabilityImpl()
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as prueba:
            await cap.charge_link(en_prueba)
        with pytest.raises(SubscriptionError) as futuro:
            await cap.charge_link(activa, period="2030-01")
    assert prueba.value.code == "en_prueba"
    assert futuro.value.code == "periodo_futuro"


@pytest.mark.asyncio
async def test_rls_forzada_en_las_tablas_de_suscripciones() -> None:
    """Gherkin «RLS forzada» (N-36)."""
    from core.db.session import get_session

    async with get_session() as s:
        filas = (
            await s.execute(
                text(
                    "select relname, relforcerowsecurity from pg_class where relname in "
                    "('pack_subscriptions_subscriptions', 'pack_subscriptions_charges', "
                    "'pack_subscriptions_avisos_cobro')"
                )
            )
        ).all()
    assert dict(filas) == {
        "pack_subscriptions_subscriptions": True,
        "pack_subscriptions_charges": True,
        "pack_subscriptions_avisos_cobro": True,
    }
