"""HU-SH-03 · Alta automática de negocios (ADR-0121), contra la BD real.

Gherkin de la HU:
- Alta automática: `subscription.activated` de un vendedor con plan Premium → negocio con
  Premium, preset del vertical e invitación al owner, en menos de 2 minutos.
- Solo vendedores: el mismo evento en un negocio fuera de la allowlist no crea nada.
- Idempotencia: el mismo evento dos veces → un solo negocio para esa referencia.
- Auditoría: el alta queda en `audit_log` con actor `provisioning`, referencia, plan y preset.
- Free: el plan Free confirmado crea el negocio en prueba de 30 días (D-16).
- «Sin el LLM» no necesita BD: `tests/test_provisioning_sin_llm.py`.

Además: aislamiento entre negocios (RLS y nunca un owner en un negocio ajeno), alta en
revisión, pedido que aún no se ve, cambio de plan, y lo que pidió la revisión de seguridad
del PR #345: el enlace va solo por correo y solo a una cuenta creada en esa alta, el aviso
por WhatsApp es igual en todos los casos, un Free por cliente y por correo, nombres sin
enlaces, cliente del pedido cotejado, y reintentar o descartar un alta en revisión.
"""

from __future__ import annotations

import itertools
import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_SEQ = itertools.count(1)


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def vendedores(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Fija la allowlist `PROVISIONING_SELLER_TENANTS` del test."""
    from core.config import get_settings

    def fijar(*valores: Any) -> None:
        monkeypatch.setenv("PROVISIONING_SELLER_TENANTS", ",".join(str(v) for v in valores))
        get_settings.cache_clear()

    yield fijar
    monkeypatch.delenv("PROVISIONING_SELLER_TENANTS", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def avisos(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Avisos por WhatsApp capturados (lo que el comprador ve)."""
    from core.gateway.event_notices import NoticeResult

    enviados: list[dict[str, Any]] = []

    async def falso(conversation_id, event_key, context, *, notice_key, default_fallback_text=""):  # type: ignore[no-untyped-def]
        enviados.append(
            {
                "conversation_id": conversation_id,
                "event": event_key,
                "context": context,
                "key": notice_key,
                "texto": default_fallback_text,
            }
        )
        return NoticeResult("texto")

    monkeypatch.setattr("core.gateway.event_notices.send_event_notice", falso)
    return enviados


@pytest.fixture
def correos(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Correo de sistema capturado (sin SMTP real)."""
    enviados: list[dict[str, str]] = []

    async def falso(to: str, subject: str, body: str) -> str:
        enviados.append({"to": to, "subject": subject, "body": body})
        return "msg-test"

    monkeypatch.setattr("core.gateway.system_email.send_system_email", falso)
    return enviados


# ------------------------------------------------------------------ helpers
async def _vendedor() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("vendedor")
    tid, _ = await onboard_tenant(
        name=f"Vendedor {slug}", slug=slug, packs=["pack_commerce", "pack_subscriptions"]
    )
    return tid


async def _pedido_de_plan(
    tid: uuid.UUID,
    *,
    plan: str | None = "premium",
    precio: int = 600_000,
    datos_alta: dict[str, Any] | None = None,
    customer_id: uuid.UUID | None = None,
    pagado: bool = True,
    activar: bool = True,
) -> tuple[str, uuid.UUID]:
    """Variante de un plan de la plataforma + pedido pagado; activarlo emite el evento."""
    from pack_commerce.models import Order, OrderItem, Product, ProductVariant
    from pack_commerce.planes import activar_planes_del_pedido

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    seq = next(_SEQ) + 100
    customer_id = customer_id or uuid.uuid4()
    atributos = {"periodicidad": "mensual", "precio_de_plan": plan} if plan else {"periodicidad": "mensual"}
    async with tenant_scope(tid):
        async with get_session() as s:
            product = Product(tenant_id=tid, name=f"Plan {plan or 'propio'}", description="Plan", has_variants=False)
            s.add(product)
            await s.flush()
            variant = ProductVariant(
                tenant_id=tid,
                product_id=product.id,
                sku=f"PLAN-{(plan or 'propio').upper()}-{seq}",
                price=precio,
                billing_period="mensual",
                trial_days=30 if precio == 0 else 0,
                attributes=atributos,
            )
            s.add(variant)
            await s.flush()
            order = Order(
                tenant_id=tid,
                conversation_id=uuid.uuid4(),
                customer_id=customer_id,
                status="abierto",
                payment_status="pagado" if pagado else "pendiente",
                payment_method="pago_en_linea",
                delivery_method="recoger_en_tienda",
                total=precio,
                idempotency_key=str(uuid.uuid4()),
                order_seq=seq,
                public_order_number=f"SH-{seq:04d}",
                delivery_full_name="Ana Pérez",
                datos_alta=datos_alta,
            )
            s.add(order)
            await s.flush()
            s.add(OrderItem(tenant_id=tid, order_id=order.id, variant_id=variant.id, quantity=1, unit_price=precio))
            await s.flush()
            if activar:
                assert await activar_planes_del_pedido(s, order) == 1
            return order.public_order_number, customer_id


async def _cliente_con_whatsapp(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(
                tenant_id=tid,
                channel="whatsapp",
                external_id=f"whatsapp:+57{uuid.uuid4().int % 10**10}",
                name="Ana",
            )
            s.add(cliente)
            await s.flush()
            s.add(Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp"))
            await s.flush()
            return cliente.id


def _datos(email: str, *, negocio: str = "Pizzería Roma", vertical: str | None = "restaurante") -> dict[str, Any]:
    datos: dict[str, Any] = {"email": email, "telefono": "+573000000000", "negocio": negocio}
    if vertical:
        datos["vertical"] = vertical
    return datos


async def _eventos(tid: uuid.UUID, referencia: str) -> list[Any]:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            filas = (
                await s.execute(
                    select(DomainEvent).where(
                        DomainEvent.tenant_id == tid, DomainEvent.topic == "subscription.activated"
                    )
                )
            ).scalars().all()
    return [e for e in filas if e.payload.get("referencia") == referencia]


async def _entregar(tid: uuid.UUID, referencia: str) -> Any:
    """Entrega el evento al handler como lo haría el despachador (mismo tenant_scope)."""
    from core.events.outbox import payload_para_handlers
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    [evento] = await _eventos(tid, referencia)
    async with tenant_scope(tid):
        await on_subscription_activated(**payload_para_handlers(evento.payload))
    return evento


async def _registro(tid: uuid.UUID, referencia: str) -> Any:
    from core.db.models import ProvisioningRequest
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return (
                await s.execute(
                    select(ProvisioningRequest).where(ProvisioningRequest.referencia == referencia)
                )
            ).scalar_one_or_none()


async def _tenant(tid: uuid.UUID) -> Any:
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as s:
        return await s.get(Tenant, tid)


async def _suscripcion_de(tid: uuid.UUID) -> Any:
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return (
                await s.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == tid))
            ).scalar_one()


async def _pagos(tid: uuid.UUID) -> list[Any]:
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tid))
                ).scalars().all()
            )


async def _invitaciones(tid: uuid.UUID) -> list[Any]:
    from core.db.models import TenantInvitation
    from core.db.session import get_session

    async with get_session() as s:
        return list(
            (await s.execute(select(TenantInvitation).where(TenantInvitation.tenant_id == tid))).scalars().all()
        )


async def _admin(email: str) -> Any:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as s:
        return (await s.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()


async def _auditoria(tid: uuid.UUID, accion: str) -> list[Any]:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(
                        select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == accion)
                    )
                ).scalars().all()
            )


def _correo() -> str:
    return f"{uniq('owner')}@correo-prueba.co"


async def _master() -> str:
    """Un master de plataforma con un correo válido (no se le puede invitar, ADR-0081)."""
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = f"{uniq('master')}@correo-prueba.co"
    async with get_session() as s:
        s.add(Admin(email=email, password_hash=hash_password("clave-master-1"), role="master"))
    return email


# ------------------------------------------------------------------ Gherkin
async def test_alta_automatica_premium_con_preset_e_invitacion(vendedores, correos) -> None:
    """Gherkin «Alta automática»: por el despachador real del outbox, en menos de 2 minutos."""
    from core.admin.invitations_service import SIN_CONTRASENA
    from core.agents.spec import enabled_packs, load_active_spec
    from core.events.core_handlers import ensure_core_handlers
    from core.shared.tenant import tenant_scope
    from core.tasks.domain_events_dispatch import dispatch_due_events

    vendedor = await _vendedor()
    vendedores(uuid.uuid4(), vendedor)
    email = _correo()
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(email))

    ensure_core_handlers()
    inicio = time.monotonic()
    await dispatch_due_events(limit=1000)
    assert time.monotonic() - inicio < 120

    [evento] = await _eventos(vendedor, referencia)
    assert evento.status == "entregado", evento.last_error
    assert evento.payload["referencia"] == referencia and evento.payload["variant_id"]
    registro = await _registro(vendedor, referencia)
    assert registro.estado == "completado", registro.motivo
    negocio = await _tenant(registro.negocio_tenant_id)
    assert negocio.name == "Pizzería Roma"
    assert negocio.slug == registro.negocio_slug and negocio.slug.startswith("pizzeria-roma-")
    assert negocio.settings["alta"]["referencia"] == referencia
    assert negocio.settings["alta"]["vertical"] == "restaurante"

    sub = await _suscripcion_de(negocio.id)
    assert (sub.plan_id, sub.status, sub.trial_ends_at) == ("premium", "active", None)
    # HU-SH-04 (ADR-0123): el período corta por calendario el mismo día que la suscripción del
    # cliente en el vendedor (antes, +30 días: los dos lados se desalineaban).
    from core.subscriptions.periodos import sumar_periodo

    assert sub.period_end == sumar_periodo(sub.period_start, "mensual", sub.dia_ancla)
    assert timedelta(days=27) < sub.period_end - datetime.now(UTC) <= timedelta(days=31)
    assert sub.provider_reference == f"plat-{negocio.slug}-{sub.period_start:%Y%m}"
    [pago] = await _pagos(negocio.id)
    assert (pago.status, pago.plan_id, int(pago.amount_cop)) == ("aprobado", "premium", 600_000)
    assert pago.provider_reference == sub.provider_reference == pago.periodo

    assert negocio.settings["preset"]["id"] == "restaurante"
    async with tenant_scope(negocio.id):
        spec = await load_active_spec(negocio.id)
    assert "pack_commerce" in enabled_packs(spec)

    owner = await _admin(email)
    assert owner is not None and owner.password_hash == SIN_CONTRASENA
    [invitacion] = await _invitaciones(negocio.id)
    assert (invitacion.role, invitacion.estado, invitacion.admin_id) == ("owner", "pendiente", owner.id)
    assert invitacion.token_nonce and invitacion.expires_at > datetime.now(UTC) + timedelta(days=6)
    [correo] = [c for c in correos if c["to"] == email]
    assert f"/invitacion#{invitacion.id}." in correo["body"]
    assert registro.detalle["invitacion"]["correo"] == "enviado"
    assert registro.invitation_id == invitacion.id


async def test_solo_vendedores_el_evento_de_otro_negocio_no_crea_nada(vendedores, correos) -> None:
    """Gherkin «Solo vendedores»."""
    from core.events.core_handlers import ensure_core_handlers
    from core.provisioning.consumer import id_del_negocio
    from core.tasks.domain_events_dispatch import dispatch_due_events

    vendedor = await _vendedor()
    no_vendedor = await _vendedor()
    vendedores(vendedor)  # solo el primero vende negocios
    email = _correo()
    referencia, _ = await _pedido_de_plan(no_vendedor, datos_alta=_datos(email))

    ensure_core_handlers()
    await dispatch_due_events(limit=1000)

    [evento] = await _eventos(no_vendedor, referencia)
    assert evento.status == "entregado"
    assert await _registro(no_vendedor, referencia) is None
    assert await _tenant(id_del_negocio(no_vendedor, referencia)) is None
    assert await _admin(email) is None
    assert correos == []


async def test_idempotencia_el_mismo_evento_dos_veces_crea_un_solo_negocio(vendedores, correos) -> None:
    """Gherkin «Idempotencia»: entrega repetida (at-least-once) y redespacho del outbox."""
    from core.db.models import DomainEvent, ProvisioningRequest, Tenant
    from core.db.session import get_session
    from core.events.core_handlers import ensure_core_handlers
    from core.shared.tenant import tenant_scope
    from core.tasks.domain_events_dispatch import dispatch_due_events

    vendedor = await _vendedor()
    vendedores(vendedor)
    email = _correo()
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(email))

    evento = await _entregar(vendedor, referencia)
    await _entregar(vendedor, referencia)
    # El despachador lo vuelve a entregar (como tras una caída antes de marcarlo).
    async with tenant_scope(vendedor):
        async with get_session() as s:
            fila = await s.get(DomainEvent, evento.id)
            fila.status = "pendiente"
            fila.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    ensure_core_handlers()
    await dispatch_due_events(limit=1000)

    registro = await _registro(vendedor, referencia)
    assert registro.estado == "completado"
    slug_vendedor = (await _tenant(vendedor)).slug
    async with get_session() as s:
        negocios = (
            await s.execute(
                select(func.count())
                .select_from(Tenant)
                .where(Tenant.settings["alta"]["vendedor"].astext == slug_vendedor)
                .where(Tenant.settings["alta"]["referencia"].astext == referencia)
            )
        ).scalar_one()
    async with tenant_scope(vendedor):
        async with get_session() as s:
            filas = (
                await s.execute(
                    select(func.count()).select_from(ProvisioningRequest).where(ProvisioningRequest.referencia == referencia)
                )
            ).scalar_one()
    assert (negocios, filas) == (1, 1)
    assert len(await _invitaciones(registro.negocio_tenant_id)) == 1
    assert len([c for c in correos if c["to"] == email]) == 1
    assert len(await _auditoria(vendedor, "negocio_aprovisionado")) == 1


async def test_auditoria_con_actor_provisioning_referencia_plan_y_preset(vendedores, correos) -> None:
    """Gherkin «Auditoría»: en el vendedor y en el negocio nuevo."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(_correo()))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    [alta] = await _auditoria(vendedor, "negocio_aprovisionado")
    assert alta.actor == "provisioning"
    assert (alta.input["referencia"], alta.input["plan"], alta.input["preset"]) == (
        referencia,
        "premium",
        "restaurante",
    )
    assert alta.output["negocio_tenant_id"] == str(registro.negocio_tenant_id)
    [creado] = await _auditoria(registro.negocio_tenant_id, "negocio_creado")
    assert creado.actor == "provisioning"
    assert (creado.input["referencia"], creado.input["plan"], creado.input["preset"]) == (
        referencia,
        "premium",
        "restaurante",
    )


async def test_free_crea_el_negocio_en_prueba_de_30_dias(vendedores, correos) -> None:
    """Gherkin «Free» (D-16). Ningún preset cabe en Free: queda pendiente, no rompe el alta."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    antes = datetime.now(UTC)
    referencia, _ = await _pedido_de_plan(vendedor, plan="free", precio=0, datos_alta=_datos(_correo()))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert registro.estado == "completado", registro.motivo
    sub = await _suscripcion_de(registro.negocio_tenant_id)
    assert (sub.plan_id, sub.status) == ("free", "trialing")
    assert timedelta(days=29, hours=23) < sub.trial_ends_at - antes < timedelta(days=30, minutes=5)
    assert registro.detalle["preset"]["estado"] == "pendiente"
    assert "plan" in registro.detalle["preset"]["detalle"]
    assert len(await _invitaciones(registro.negocio_tenant_id)) == 1


# ------------------------------------------------------------------ aislamiento entre negocios
async def test_el_registro_del_alta_solo_lo_ve_el_vendedor(vendedores, correos) -> None:
    """RLS forzada: ni el negocio creado ni otro negocio ven la fila del vendedor."""
    vendedor = await _vendedor()
    otro = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(_correo()))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert registro is not None
    assert await _registro(otro, referencia) is None
    assert await _registro(registro.negocio_tenant_id, referencia) is None


async def test_nunca_agrega_un_owner_a_un_negocio_ajeno(vendedores, correos) -> None:
    """Si el slug del alta ya es de otro negocio, revisión: sin invitación ni cambio de plan."""
    from core.alta_negocio import onboard_tenant
    from core.provisioning.consumer import id_del_negocio, slug_del_negocio

    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(_correo()), activar=False)
    ajeno, _ = await onboard_tenant(name="Ajeno", slug=slug_del_negocio("Pizzería Roma", vendedor, referencia))
    # Ahora sí se activa el plan del pedido (emite el evento).
    from pack_commerce.models import Order
    from pack_commerce.planes import activar_planes_del_pedido

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(vendedor):
        async with get_session() as s:
            order = (await s.execute(select(Order).where(Order.public_order_number == referencia))).scalar_one()
            assert await activar_planes_del_pedido(s, order) == 1
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert registro.estado == "revision"
    assert "otro negocio" in registro.motivo
    assert await _invitaciones(ajeno) == []
    assert (await _suscripcion_de(ajeno)).plan_id == "free"
    assert "alta" not in ((await _tenant(ajeno)).settings or {})
    assert await _tenant(id_del_negocio(vendedor, referencia)) is None
    [revision] = await _auditoria(vendedor, "aprovisionamiento_en_revision")
    assert revision.actor == "provisioning"


# ------------------------------------------------------------------ revisión y reintentos
async def test_sin_correo_queda_en_revision_sin_crear_nada(vendedores, correos) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta={"negocio": "Sin correo"})
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert (registro.estado, registro.negocio_tenant_id) == ("revision", None)
    assert "correo" in registro.motivo
    [revision] = await _auditoria(vendedor, "aprovisionamiento_en_revision")
    assert revision.input["referencia"] == referencia


async def test_correo_de_un_master_queda_en_revision_antes_de_crear_el_negocio(vendedores, correos) -> None:
    from core.provisioning.consumer import id_del_negocio

    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(await _master()))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert registro.estado == "revision"
    assert "no se puede invitar" in registro.motivo
    assert await _tenant(id_del_negocio(vendedor, referencia)) is None


async def test_pedido_que_aun_no_se_ve_se_reintenta_y_termina_en_revision(vendedores, correos) -> None:
    from core.provisioning import AltaTransitoria
    from core.provisioning.consumer import MAX_INTENTOS, aprovisionar
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    payload = {"subscription_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()), "referencia": "SH-9999"}
    async with tenant_scope(vendedor):
        for _ in range(MAX_INTENTOS - 1):
            with pytest.raises(AltaTransitoria):
                await aprovisionar(vendedor, payload)
        registro = await aprovisionar(vendedor, payload)
    assert registro is not None
    assert (registro.estado, registro.intentos) == ("revision", MAX_INTENTOS)
    assert "todavía no es visible" in registro.motivo


async def test_plan_que_no_es_de_la_plataforma_se_omite(vendedores, correos) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, plan=None, precio=50_000, datos_alta=_datos(_correo()))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert (registro.estado, registro.negocio_tenant_id) == ("omitido", None)


async def test_cambio_de_plan_no_crea_otro_negocio(vendedores, correos) -> None:
    """Free → Premium en la misma suscripción: un solo negocio; el cambio queda auditado."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    email = _correo()
    free, cliente = await _pedido_de_plan(vendedor, plan="free", precio=0, datos_alta=_datos(email))
    await _entregar(vendedor, free)
    premium, _ = await _pedido_de_plan(vendedor, customer_id=cliente, datos_alta=_datos(email))
    [evento] = await _eventos(vendedor, premium)
    assert evento.payload["cambio_de_plan"] is True
    await _entregar(vendedor, premium)

    registro = await _registro(vendedor, free)
    assert registro.estado == "completado"
    assert await _registro(vendedor, premium) is None
    [cambio] = await _auditoria(vendedor, "aprovisionamiento_cambio_de_plan")
    assert cambio.input["referencia"] == premium
    assert cambio.output["negocio_tenant_id"] == str(registro.negocio_tenant_id)
    assert len(await _auditoria(vendedor, "negocio_aprovisionado")) == 1


# ------------------------------------------------------------------ owner e invitación
async def test_cuenta_existente_recibe_invitacion_sin_enlace(vendedores, correos) -> None:
    """ADR-0081: no se toca la contraseña de una cuenta existente ni se la vincula sola."""
    from core.admin.auth import hash_password, verify_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = _correo()
    async with get_session() as s:
        s.add(Admin(email=email, password_hash=hash_password("clave-propia-1"), role="colaborador"))
    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(email))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    [invitacion] = await _invitaciones(registro.negocio_tenant_id)
    assert (invitacion.estado, invitacion.token_nonce) == ("pendiente", None)
    owner = await _admin(email)
    assert verify_password("clave-propia-1", owner.password_hash)
    async with get_session() as s:
        vinculos = (
            await s.execute(select(TenantUser).where(TenantUser.tenant_id == registro.negocio_tenant_id))
        ).scalars().all()
    assert vinculos == []
    [correo] = [c for c in correos if c["to"] == email]
    assert "acepta la invitación" in correo["body"] and "#" not in correo["body"]


async def test_correo_ajeno_sin_cuenta_el_enlace_solo_va_por_correo(vendedores, correos, avisos) -> None:
    """Quien compra con el correo de otro no recibe el enlace: va al correo, y por WhatsApp
    sale un aviso sin token, con el correo enmascarado."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    cliente = await _cliente_con_whatsapp(vendedor)
    email = _correo()
    referencia, _ = await _pedido_de_plan(vendedor, customer_id=cliente, datos_alta=_datos(email))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert registro.estado == "completado", registro.motivo
    assert registro.detalle["owner"]["creada"] is True
    [correo] = [c for c in correos if c["to"] == email]
    assert f"/invitacion#{registro.invitation_id}." in correo["body"]
    assert "Pizzería" not in correo["subject"]
    [aviso] = avisos
    assert aviso["event"] == "provisioning.invitation"
    assert aviso["key"] == f"provisioning_aviso:{registro.id}"
    assert aviso["context"]["invitacion"] == {"correo": email[0] + "***@correo-prueba.co"}
    assert "{invitacion.enlace}" not in aviso["texto"]
    plano = str(aviso)
    assert "/invitacion#" not in plano and email not in plano
    assert registro.detalle["invitacion"]["whatsapp"] == "texto"


async def test_cuenta_existente_sin_contrasena_no_recibe_enlace(vendedores, correos, avisos) -> None:
    """Una cuenta que quedó sin contraseña por un alta anterior no se toma con un enlace
    nuevo: la invitación va en la app y se entra recuperando la contraseña por correo."""
    from core.admin.invitations_service import SIN_CONTRASENA
    from core.db.models import Admin
    from core.db.session import get_session

    email = _correo()
    async with get_session() as s:
        s.add(Admin(email=email, password_hash=SIN_CONTRASENA, role="colaborador"))
    vendedor = await _vendedor()
    vendedores(vendedor)
    cliente = await _cliente_con_whatsapp(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, customer_id=cliente, datos_alta=_datos(email))
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    assert registro.detalle["owner"]["creada"] is False
    [invitacion] = await _invitaciones(registro.negocio_tenant_id)
    assert (invitacion.token_nonce, invitacion.expires_at) == (None, None)
    [correo] = [c for c in correos if c["to"] == email]
    assert "#" not in correo["body"] and "acepta la invitación" in correo["body"]
    assert len(avisos) == 1


async def test_dos_altas_con_el_mismo_correo_solo_la_primera_lleva_enlace(vendedores, correos) -> None:
    """La cuenta la crea la primera alta; la segunda la encuentra hecha: sin enlace."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    email = _correo()
    primera, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(email, negocio="Primero"))
    segunda, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(email, negocio="Segundo"))
    await _entregar(vendedor, primera)
    await _entregar(vendedor, segunda)

    uno, dos = await _registro(vendedor, primera), await _registro(vendedor, segunda)
    assert (uno.estado, dos.estado) == ("completado", "completado")
    assert (uno.detalle["owner"]["creada"], dos.detalle["owner"]["creada"]) == (True, False)
    [inv_uno] = await _invitaciones(uno.negocio_tenant_id)
    [inv_dos] = await _invitaciones(dos.negocio_tenant_id)
    assert inv_uno.token_nonce and inv_dos.token_nonce is None
    cuerpos = [c["body"] for c in correos if c["to"] == email]
    assert sum("/invitacion#" in c for c in cuerpos) == 1


async def test_el_aviso_es_el_mismo_aunque_el_alta_quede_en_revision(vendedores, correos, avisos) -> None:
    """Sin enumeración: el correo de un master deja el alta en revisión, pero el comprador ve
    el mismo aviso que en un alta normal (mismo texto y mismas variables)."""
    vendedor = await _vendedor()
    vendedores(vendedor)
    normal_cliente = await _cliente_con_whatsapp(vendedor)
    master_cliente = await _cliente_con_whatsapp(vendedor)
    correo_master = await _master()
    normal, _ = await _pedido_de_plan(vendedor, customer_id=normal_cliente, datos_alta=_datos(_correo()))
    master, _ = await _pedido_de_plan(vendedor, customer_id=master_cliente, datos_alta=_datos(correo_master))
    await _entregar(vendedor, normal)
    await _entregar(vendedor, master)

    assert (await _registro(vendedor, normal)).estado == "completado"
    revision = await _registro(vendedor, master)
    assert revision.estado == "revision" and "no se puede invitar" in revision.motivo
    uno, dos = avisos
    assert uno["texto"] == dos["texto"]
    assert set(uno["context"]) == set(dos["context"])
    assert set(uno["context"]["invitacion"]) == set(dos["context"]["invitacion"]) == {"correo"}
    assert dos["context"]["invitacion"]["correo"] == "m***@correo-prueba.co"
    assert not [c for c in correos if c["to"] == correo_master]


async def test_un_free_repetido_del_mismo_cliente_o_correo_queda_en_revision(vendedores, correos) -> None:
    """Free → cancelar → Free (u otro número con el mismo correo) no crea N negocios."""
    from core.provisioning.consumer import aprovisionar
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    email = _correo()
    primero, cliente = await _pedido_de_plan(vendedor, plan="free", precio=0, datos_alta=_datos(email))
    await _entregar(vendedor, primero)
    assert (await _registro(vendedor, primero)).estado == "completado"

    # Mismo correo desde otro cliente.
    otro, _ = await _pedido_de_plan(vendedor, plan="free", precio=0, datos_alta=_datos(email))
    await _entregar(vendedor, otro)
    assert (await _registro(vendedor, otro)).estado == "revision"

    # Mismo cliente (después de cancelar la primera suscripción) con otro correo.
    tercero, _ = await _pedido_de_plan(
        vendedor, plan="free", precio=0, customer_id=cliente, datos_alta=_datos(_correo()), activar=False
    )
    async with tenant_scope(vendedor):
        fila = await aprovisionar(
            vendedor,
            {"subscription_id": str(uuid.uuid4()), "customer_id": str(cliente), "referencia": tercero},
        )
    assert fila is not None and fila.estado == "revision"
    assert "Free" in fila.motivo


async def test_un_nombre_con_enlace_no_llega_al_negocio_ni_al_correo(vendedores, correos) -> None:
    vendedor = await _vendedor()
    vendedores(vendedor)
    email = _correo()
    referencia, _ = await _pedido_de_plan(
        vendedor, datos_alta=_datos(email, negocio="Ingresa a www.pagos-falsos.co")
    )
    await _entregar(vendedor, referencia)

    registro = await _registro(vendedor, referencia)
    negocio = await _tenant(registro.negocio_tenant_id)
    assert negocio.name == "Ana Pérez"  # el nombre del comprador
    [correo] = [c for c in correos if c["to"] == email]
    assert "pagos-falsos" not in correo["body"] and "pagos-falsos" not in correo["subject"]


async def test_si_el_cliente_del_pedido_no_es_el_de_la_suscripcion_va_a_revision(vendedores, correos) -> None:
    from core.provisioning.consumer import aprovisionar, id_del_negocio
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(_correo()), activar=False)
    async with tenant_scope(vendedor):
        fila = await aprovisionar(
            vendedor,
            {"subscription_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()), "referencia": referencia},
        )
    assert fila is not None and fila.estado == "revision"
    assert "cliente" in fila.motivo
    assert await _tenant(id_del_negocio(vendedor, referencia)) is None


async def test_el_master_reintenta_o_descarta_un_alta_en_revision(vendedores, correos) -> None:
    """Runbook `docs/runbooks/altas-en-revision.md`: se corrige el dato y se reintenta; o se descarta."""
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.provisioning.consumer import descartar_alta, reintentar_alta
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    sin_correo, _ = await _pedido_de_plan(vendedor, datos_alta={"negocio": "Sin correo"})
    otro, _ = await _pedido_de_plan(vendedor, datos_alta={"negocio": "Otro sin correo"})
    await _entregar(vendedor, sin_correo)
    await _entregar(vendedor, otro)
    assert (await _registro(vendedor, sin_correo)).estado == "revision"

    async with tenant_scope(vendedor):
        async with get_session() as s:
            pedido = (await s.execute(select(Order).where(Order.public_order_number == sin_correo))).scalar_one()
            pedido.datos_alta = {**pedido.datos_alta, "email": _correo()}
    reintentada = await reintentar_alta(vendedor, sin_correo)
    assert reintentada.estado == "completado", reintentada.motivo
    with pytest.raises(ValueError):
        await reintentar_alta(vendedor, sin_correo)  # ya no está en revisión

    descartada = await descartar_alta(vendedor, otro, "pedido de prueba")
    assert descartada.estado == "omitido" and "pedido de prueba" in descartada.motivo
    await _entregar(vendedor, otro)  # un evento repetido no la reabre
    assert (await _registro(vendedor, otro)).estado == "omitido"
    assert len(await _auditoria(vendedor, "aprovisionamiento_descartado")) == 1


async def test_reintento_con_el_correo_corregido_no_manda_el_enlace_a_otro_correo(vendedores, monkeypatch) -> None:
    """La cuenta anotada en el alta es la del correo A. Si el master corrige el pedido al
    correo B y reintenta, el alta va a revisión: el enlace de la cuenta A nunca va a B."""
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.provisioning import AltaTransitoria
    from core.provisioning.consumer import MAX_INTENTOS, reintentar_alta
    from core.provisioning.owner import CORREO_CAMBIADO
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    vendedores(vendedor)
    correo_a, correo_b = _correo(), _correo()
    referencia, _ = await _pedido_de_plan(vendedor, datos_alta=_datos(correo_a))

    async def smtp_caido(to: str, subject: str, body: str) -> str:
        raise OSError("smtp caído")

    # El alta crea la cuenta de A, pero el correo no sale: al tercer intento, revisión.
    monkeypatch.setattr("core.gateway.system_email.send_system_email", smtp_caido)
    for _ in range(MAX_INTENTOS - 1):
        with pytest.raises(AltaTransitoria):
            await _entregar(vendedor, referencia)
    await _entregar(vendedor, referencia)
    registro = await _registro(vendedor, referencia)
    assert registro.estado == "revision"
    assert registro.detalle["owner"]["creada"] is True

    # El master corrige el correo del pedido a B y reintenta, con el SMTP ya funcionando.
    enviados: list[dict[str, str]] = []

    async def smtp_ok(to: str, subject: str, body: str) -> str:
        enviados.append({"to": to, "subject": subject, "body": body})
        return "msg-test"

    monkeypatch.setattr("core.gateway.system_email.send_system_email", smtp_ok)
    async with tenant_scope(vendedor):
        async with get_session() as s:
            pedido = (await s.execute(select(Order).where(Order.public_order_number == referencia))).scalar_one()
            pedido.datos_alta = {**pedido.datos_alta, "email": correo_b}
    fila = await reintentar_alta(vendedor, referencia)

    assert fila.estado == "revision"
    assert fila.motivo == CORREO_CAMBIADO
    assert enviados == []  # ni el enlace de la cuenta A a B, ni nada a A
    assert await _admin(correo_b) is None
