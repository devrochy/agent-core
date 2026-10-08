"""HU-SUB-07 · cambio de plan con precio del servidor (ADR-0111), contra la BD real.

Gherkin: upgrade prorrateado que cambia al pagar, downgrade programado sin reembolso,
referencia del pedido (idempotente) y varias suscripciones por cliente según el negocio.
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
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(name="Negocio", slug=uniq("cplan"), packs=packs or ["pack_subscriptions"], plan="premium")
    return tid


async def _sub(tid, plan, **campos) -> uuid.UUID:
    from pack_subscriptions.capability import crear_suscripcion_para
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        sub = await crear_suscripcion_para(customer_id=uuid.uuid4(), variante_id=uuid.uuid4(), plan=plan)
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
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    inicio, fin = ahora - timedelta(days=15), ahora + timedelta(days=15)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=inicio, proximo_cargo=fin)
    variante_premium = str(uuid.uuid4())
    async with tenant_scope(tid):
        vista = await SubscriptionCapabilityImpl().change_plan(sid, variant_id=variante_premium, plan=PREMIUM)
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
    assert str(sub.variante_id) == variante_premium
    assert sub.proximo_cargo == fin  # el período sigue igual


async def test_desde_free_cobra_el_plan_completo_y_arranca_periodo() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import Subscription

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    sid = await _sub(tid, FREE)
    async with tenant_scope(tid):
        vista = await SubscriptionCapabilityImpl().change_plan(sid, variant_id=str(uuid.uuid4()), plan=BASIC)
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
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    fin = ahora + timedelta(days=10)
    sid = await _sub(tid, PREMIUM, periodo_actual_inicio=ahora - timedelta(days=20), proximo_cargo=fin)
    async with tenant_scope(tid):
        vista = await SubscriptionCapabilityImpl().change_plan(sid, variant_id=str(uuid.uuid4()), plan=BASIC)
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


async def test_mismo_pedido_dos_veces_una_sola_suscripcion() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente = uuid.uuid4()
    variante = uuid.uuid4()
    async with tenant_scope(tid):
        a = await crear_suscripcion_para(customer_id=cliente, variante_id=variante, plan=BASIC, reference="OC-0007")
        b = await crear_suscripcion_para(customer_id=uuid.uuid4(), variante_id=variante, plan=BASIC, reference="OC-0007")
        vistas = await SubscriptionCapabilityImpl().list_for_customer(cliente)
    assert a.id == b.id
    assert vistas[0]["referencia"] == "OC-0007"


async def test_un_pedido_con_dos_planes_crea_dos_suscripciones() -> None:
    """Auditoría: la idempotencia es por pedido Y plan (antes la 2.ª línea devolvía la 1.ª)."""
    from pack_subscriptions.capability import crear_suscripcion_para

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente = uuid.uuid4()
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_subscriptions", {"varias_por_cliente": True})
        a = await crear_suscripcion_para(customer_id=cliente, variante_id=uuid.uuid4(), plan=BASIC, reference="OC-0008")
        b = await crear_suscripcion_para(customer_id=cliente, variante_id=uuid.uuid4(), plan=PREMIUM, reference="OC-0008")
    assert a.id != b.id


async def test_varias_por_cliente_segun_el_negocio() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente = uuid.uuid4()
    async with tenant_scope(tid):
        assert await SubscriptionCapabilityImpl().permite_varias() is False
        await plugin_manager.save_config("pack_subscriptions", {"varias_por_cliente": True})
        assert await SubscriptionCapabilityImpl().permite_varias() is True
        a = await crear_suscripcion_para(customer_id=cliente, variante_id=uuid.uuid4(), plan=BASIC)
        b = await crear_suscripcion_para(customer_id=cliente, variante_id=uuid.uuid4(), plan=PREMIUM)
        vistas = await SubscriptionCapabilityImpl().list_for_customer(cliente)
    assert a.id != b.id
    assert {v["plan"] for v in vistas} == {"Basic", "Premium"}


async def test_un_cargo_de_cambio_impago_no_vence_la_suscripcion() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.jobs import _marcar_vencidas
    from pack_subscriptions.models import Subscription

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=ahora - timedelta(days=15), proximo_cargo=ahora + timedelta(days=15))
    async with tenant_scope(tid):
        await SubscriptionCapabilityImpl().change_plan(sid, variant_id=str(uuid.uuid4()), plan=PREMIUM)
    await _marcar_vencidas(ahora + timedelta(days=12))
    assert (await _leer(tid, Subscription, sid)).estado == "activa"


async def test_commerce_no_vende_un_segundo_plan_pago_y_ofrece_el_cambio() -> None:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem
    from pack_commerce.skills import OrderSkill
    from pack_subscriptions.capability import crear_suscripcion_para

    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope
    from core.subscriptions import guardar_proveedor

    tid = await _negocio(["pack_commerce", "pack_subscriptions"])
    await guardar_proveedor(tid, "pack_subscriptions")
    cliente = uuid.uuid4()
    async with tenant_scope(tid):
        await crear_suscripcion_para(customer_id=cliente, variante_id=uuid.uuid4(), plan=BASIC)
        async with get_session() as s:
            producto = Product(tenant_id=tid, name="Premium")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(tenant_id=tid, product_id=producto.id, sku="PLAN-PREMIUM", price=600000, billing_period="mensual")
            s.add(variante)
            await s.flush()
            cotizacion = Quote(tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=cliente, subtotal=600000, discount=0, total=600000, status="aceptada")
            s.add(cotizacion)
            await s.flush()
            s.add(QuoteItem(tenant_id=tid, quote_id=cotizacion.id, variant_id=variante.id, quantity=1, unit_price=600000))
            await s.flush()
            quote_id = str(cotizacion.id)
        ctx = ToolContext(conversation_id=uuid.uuid4(), customer_id=cliente, idempotency_key=f"t:{uuid.uuid4().hex[:6]}", channel="whatsapp")
        resultado = await OrderSkill().crear_pedido(
            {"quote_id": quote_id, "payment_method": "pago_en_linea", "delivery_method": "recoger_en_tienda",
             "customer_data": {"full_name": "Ana", "id_document": "1", "phone": "300", "email": "a@example.com"}},
            ctx,
        )
    assert resultado.get("error") == "ya_tiene_suscripcion", resultado
    assert "sub_cambiar_plan" in resultado["hint"]


async def test_el_catalogo_de_commerce_da_el_precio_del_servidor() -> None:
    from pack_commerce.models import Product, ProductVariant
    from pack_commerce.planes import CatalogoDePlanes

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio(["pack_commerce"])
    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name="Premium")
            s.add(producto)
            await s.flush()
            s.add(ProductVariant(tenant_id=tid, product_id=producto.id, sku="PLAN-PREMIUM", price=600000, billing_period="mensual", trial_days=30))
            s.add(ProductVariant(tenant_id=tid, product_id=producto.id, sku="CAMISETA", price=50000))
        encontrado = await CatalogoDePlanes().plan_por_codigo("PLAN-PREMIUM")
        no_plan = await CatalogoDePlanes().plan_por_codigo("CAMISETA")
    assert encontrado is not None
    _, plan = encontrado
    assert (plan["precio"], plan["trial_dias"]) == (600000.0, 0)  # D-16: la prueba de un plan pago se ignora
    assert no_plan is None


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
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import Subscription

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=ahora - timedelta(days=15), proximo_cargo=ahora + timedelta(days=15))
    async with tenant_scope(tid):
        cap = SubscriptionCapabilityImpl()
        await cap.change_plan(sid, variant_id=str(uuid.uuid4()), plan=PREMIUM)
        # Pide otro destino: el primer cargo queda anulado.
        otro = {**PREMIUM, "plan_code": "PLAN-PRO", "nombre": "Pro", "precio": 400000.0}
        await cap.change_plan(sid, variant_id=str(uuid.uuid4()), plan=otro)
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
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import Subscription

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    fin = ahora + timedelta(days=1)
    sid = await _sub(tid, BASIC, periodo_actual_inicio=ahora - timedelta(days=29), proximo_cargo=fin)
    async with tenant_scope(tid):
        await SubscriptionCapabilityImpl().change_plan(sid, variant_id=str(uuid.uuid4()), plan=PREMIUM)
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
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    tid = await _negocio()
    sid = await _sub(tid, BASIC, estado=estado_inicial)
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as exc:
            await SubscriptionCapabilityImpl().change_plan(sid, variant_id=str(uuid.uuid4()), plan=plan_nuevo)
    assert exc.value.code == codigo


async def test_de_free_a_otro_plan_gratis_no_se_permite() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    tid = await _negocio()
    sid = await _sub(tid, FREE)
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as exc:
            await SubscriptionCapabilityImpl().change_plan(
                sid, variant_id=str(uuid.uuid4()), plan={**FREE, "plan_code": "FREE-ANUAL", "periodicidad": "anual"}
            )
    assert exc.value.code == "plan_gratis"


async def test_comprar_el_mismo_plan_vigente_se_frena() -> None:
    """Auditoría: un pedido del plan que ya tiene cobraría sin dar nada; se ofrece el link."""
    from pack_commerce.models import Product, ProductVariant
    from pack_commerce.planes import segundo_plan_bloqueado
    from pack_subscriptions.capability import crear_suscripcion_para

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.subscriptions import guardar_proveedor

    tid = await _negocio(["pack_commerce", "pack_subscriptions"])
    await guardar_proveedor(tid, "pack_subscriptions")
    cliente = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name="Basic")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(tenant_id=tid, product_id=producto.id, sku="PLAN-BASIC", price=200000, billing_period="mensual")
            s.add(variante)
            await s.flush()
            variante_id = variante.id
        await crear_suscripcion_para(customer_id=cliente, variante_id=variante_id, plan=BASIC)
        bloqueo = await segundo_plan_bloqueado(cliente, [variante_id])
    assert bloqueo is not None and bloqueo["error"] == "ya_tiene_este_plan"
    assert "sub_link_pago" in bloqueo["hint"]
