"""HU-SUB-07 · cambio de plan con precio del servidor (ADR-0111), contra la BD real.

Gherkin: upgrade prorrateado que cambia al pagar, downgrade programado sin reembolso y varias
suscripciones por cliente según el negocio. Los planes son del catálogo de `pack_subscriptions`
(SPEC-43): la referencia de un pedido de commerce y el bloqueo de un segundo plan al vender ya no existen
(la inscripción repetida está en `test_subscription_inscripcion.py`).
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

BASIC = {"plan_code": "PLAN-BASIC", "nombre": "Basic", "precio": 200000.0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0}
PREMIUM = {"plan_code": "PLAN-PREMIUM", "nombre": "Premium", "precio": 600000.0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0}
FREE = {"plan_code": "PLAN-FREE", "nombre": "Free", "precio": 0.0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30}


async def _negocio(packs: list[str] | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(name="Negocio", slug=uniq("cplan"), packs=packs or ["pack_subscriptions"], plan="premium")
    return tid


async def _id_de(tid, plan: dict) -> str:
    """Id del plan del catálogo con el código del snapshot (se crea una vez por negocio)."""
    from pack_subscriptions import planes

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        existente = await planes.por_codigo(plan["plan_code"], solo_activos=False)
        if existente is None:
            existente = await planes.crear(
                planes.DatosDePlan(
                    codigo=plan["plan_code"], nombre=plan["nombre"], precio=Decimal(str(plan["precio"])),
                    periodicidad=plan["periodicidad"], moneda=plan["moneda"], trial_dias=int(plan.get("trial_dias", 0)),
                )
            )
        return str(existente.id)


async def _cambiar(tid, sid, plan: dict):
    """`change_plan` hacia el plan del catálogo con el código del snapshot."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope

    plan_id = await _id_de(tid, plan)
    async with tenant_scope(tid):
        return await SubscriptionCapabilityImpl().change_plan(sid, plan_id=plan_id, plan=plan)


async def _sub(tid, plan, **campos) -> uuid.UUID:
    from pack_subscriptions.capability import crear_suscripcion_para
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    plan_id = uuid.UUID(await _id_de(tid, plan))
    async with tenant_scope(tid):
        sub = await crear_suscripcion_para(customer_id=uuid.uuid4(), plan_id=plan_id)
        if campos:
            async with get_session() as s:
                fila = await s.get(Subscription, sub.id)
                for k, v in campos.items():
                    setattr(fila, k, v)
    return sub.id


async def _leer(tid, modelo, id_):
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await s.get(modelo, id_)


async def _pagar(tid, cargo_id, monto) -> None:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()
    modulo = plugin_manager.get("pack_subscriptions")
    async with tenant_scope(tid):
        await modulo._on_payment_confirmed(
            reference=f"sub-{cargo_id}", provider="simulado", provider_reference="sim", amount=float(monto)
        )


async def test_upgrade_cobra_la_diferencia_prorrateada_y_cambia_al_pagar() -> None:
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    inicio, fin = ahora - timedelta(days=15), ahora + timedelta(days=15)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=inicio, proximo_cargo=fin)
    plan_premium = await _id_de(tid, PREMIUM)
    vista = await _cambiar(tid, sid, PREMIUM)
    assert vista["cambio"] == "pendiente_pago"
    assert vista["cargo_cambio"]["monto"] == 200000.0  # 400.000 × 15/30
    sub = await _leer(tid, Subscription, sid)
    assert sub.concepto == "Basic"  # todavía no cambia

    async with tenant_scope(tid):
        from core.db.session import get_session

        async with get_session() as s:
            cargo = (
                await s.execute(select(SubscriptionCharge).where(SubscriptionCharge.subscription_id == sid))
            ).scalar_one()
    assert (cargo.tipo, cargo.estado) == ("cambio_plan", "pendiente")
    await _pagar(tid, cargo.id, cargo.monto)
    sub = await _leer(tid, Subscription, sid)
    assert (sub.concepto, Decimal(sub.valor_periodo)) == ("Premium", Decimal("600000"))
    assert str(sub.plan_id) == plan_premium
    assert sub.proximo_cargo == fin  # el período sigue igual


async def test_desde_free_cobra_el_plan_completo_y_arranca_periodo() -> None:
    from pack_subscriptions.models import Subscription

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    sid = await _sub(tid, FREE)
    vista = await _cambiar(tid, sid, BASIC)
    assert vista["cargo_cambio"]["monto"] == 200000.0
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as s:
            cargo = (
                await s.execute(select(SubscriptionCharge).where(SubscriptionCharge.subscription_id == sid))
            ).scalar_one()
    await _pagar(tid, cargo.id, cargo.monto)
    sub = await _leer(tid, Subscription, sid)
    assert (sub.estado, sub.concepto, sub.trial_termina) == ("activa", "Basic", None)
    assert sub.proximo_cargo > datetime.now(UTC) + timedelta(days=27)


async def test_downgrade_queda_programado_y_el_proximo_cargo_sale_con_el_precio_nuevo() -> None:
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    fin = ahora + timedelta(days=10)
    sid = await _sub(tid, PREMIUM, periodo_actual_inicio=ahora - timedelta(days=20), proximo_cargo=fin)
    vista = await _cambiar(tid, sid, BASIC)
    assert vista["cambio"] == "programado"
    sub = await _leer(tid, Subscription, sid)
    assert (sub.concepto, Decimal(sub.valor_periodo)) == ("Premium", Decimal("600000"))  # sin reembolso
    assert sub.cambio_programado is not None

    await _generar_cargos(fin - timedelta(days=1))
    sub = await _leer(tid, Subscription, sid)
    assert (sub.concepto, sub.cambio_programado) == ("Basic", None)
    async with tenant_scope(tid):
        async with get_session() as s:
            cargo = (
                await s.execute(select(SubscriptionCharge).where(SubscriptionCharge.subscription_id == sid))
            ).scalar_one()
    assert Decimal(cargo.monto) == Decimal("200000")


async def test_varias_por_cliente_segun_el_negocio() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente = uuid.uuid4()
    basic, premium = uuid.UUID(await _id_de(tid, BASIC)), uuid.UUID(await _id_de(tid, PREMIUM))
    async with tenant_scope(tid):
        assert await SubscriptionCapabilityImpl().permite_varias() is False
        await plugin_manager.save_config("pack_subscriptions", {"varias_por_cliente": True})
        assert await SubscriptionCapabilityImpl().permite_varias() is True
        a = await crear_suscripcion_para(customer_id=cliente, plan_id=basic)
        b = await crear_suscripcion_para(customer_id=cliente, plan_id=premium)
        vistas = await SubscriptionCapabilityImpl().list_for_customer(cliente)
    assert a.id != b.id
    assert {v["plan"] for v in vistas} == {"Basic", "Premium"}


async def test_un_cargo_de_cambio_impago_no_vence_la_suscripcion() -> None:
    from pack_subscriptions.jobs import _marcar_vencidas
    from pack_subscriptions.models import Subscription


    tid = await _negocio()
    ahora = datetime.now(UTC)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=ahora - timedelta(days=15), proximo_cargo=ahora + timedelta(days=15))
    await _cambiar(tid, sid, PREMIUM)
    await _marcar_vencidas(ahora + timedelta(days=12))
    assert (await _leer(tid, Subscription, sid)).estado == "activa"


# ------------------------------------------------------------- auditoría (dinero)
async def _cargo_de_cambio(tid, sid):
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return (
                await s.execute(
                    select(SubscriptionCharge)
                    .where(SubscriptionCharge.subscription_id == sid, SubscriptionCharge.tipo == "cambio_plan")
                    .order_by(SubscriptionCharge.created_at)
                )
            ).scalars().all()


async def test_pagar_un_cambio_anulado_no_aplica_el_plan() -> None:
    from pack_subscriptions.models import Subscription

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=ahora - timedelta(days=15), proximo_cargo=ahora + timedelta(days=15))
    await _cambiar(tid, sid, PREMIUM)
    # Pide otro destino: el primer cargo queda anulado.
    otro = {**PREMIUM, "plan_code": "PLAN-PRO", "nombre": "Pro", "precio": 400000.0}
    await _cambiar(tid, sid, otro)
    cargos = await _cargo_de_cambio(tid, sid)
    assert [c.estado for c in cargos] == ["anulado", "pendiente"]
    await _pagar(tid, cargos[0].id, cargos[0].monto)
    assert (await _leer(tid, Subscription, sid)).concepto == "Basic"
    async with tenant_scope(tid):
        async with get_session() as s:
            alertas = (
                await s.execute(select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == "subs_pago_sin_aplicar"))
            ).scalars().all()
    assert len(alertas) == 1


async def test_pagar_un_cambio_de_un_periodo_cerrado_no_aplica_el_plan() -> None:
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import Subscription


    tid = await _negocio()
    ahora = datetime.now(UTC)
    fin = ahora + timedelta(days=1)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=ahora - timedelta(days=29), proximo_cargo=fin)
    await _cambiar(tid, sid, PREMIUM)
    cargo = (await _cargo_de_cambio(tid, sid))[0]
    await _generar_cargos(fin)  # el cron pasa de período: el cambio pendiente se anula
    await _pagar(tid, cargo.id, cargo.monto)
    sub = await _leer(tid, Subscription, sid)
    assert (sub.concepto, Decimal(sub.valor_periodo)) == ("Basic", Decimal("200000"))


@pytest.mark.parametrize(
    ("estado_inicial", "plan_nuevo", "codigo"),
    [
        ("pausada", PREMIUM, "estado_no_permite_cambio"),
        ("vencida", PREMIUM, "estado_no_permite_cambio"),
        ("activa", {**PREMIUM, "periodicidad": "trimestral"}, "periodicidad_distinta"),
    ],
)
async def test_cambios_que_no_se_permiten(estado_inicial, plan_nuevo, codigo) -> None:

    from core.subscriptions import SubscriptionError

    tid = await _negocio()
    sid = await _sub(tid, BASIC, estado=estado_inicial)
    with pytest.raises(SubscriptionError) as exc:
        await _cambiar(tid, sid, plan_nuevo)
    assert exc.value.code == codigo


async def test_de_free_a_otro_plan_gratis_no_se_permite() -> None:

    from core.subscriptions import SubscriptionError

    tid = await _negocio()
    sid = await _sub(tid, FREE)
    with pytest.raises(SubscriptionError) as exc:
        await _cambiar(tid, sid, {**FREE, "plan_code": "FREE-ANUAL", "periodicidad": "anual"})
    assert exc.value.code == "plan_gratis"


