"""HU-SH-04 · Cobro de plataforma por dos canales con fuente única (ADR-0123), contra la BD real.

Gherkin de la HU (cada uno con su test):
- Pago por el asistente: el cargo del período `plat-<slug>-<AAAAMM>` en el vendedor, pagado
  por el link → `tenant_subscriptions` del negocio extiende su período.
- Pago por el panel: el mismo período pagado desde `/plan` → el cargo del canal asistente queda
  pagado y el asistente no lo vuelve a cobrar.
- Sin doble cobro: un período pagado por un canal → el otro responde «ya pagado».
- Renovación: un período que vence en 5 días → el job genera el cobro del siguiente con la
  referencia del período y avisa por el canal preferido.
- Conciliación: una diferencia entre los dos lados → el job diario alerta con el detalle.
- Plan creado por el alta: vence, entra en la renovación y no queda bloqueado si paga.
- Cambio de plan desde el asistente: un Free del alta que compra un plan pago cambia de plan
  sin crear otro negocio y se aplica el preset pendiente (ADR-0110 § 3.1).

Además: aislamiento entre negocios y vendedores, pago doble y el enrutador de eventos de
Wompi para una cuenta compartida (riesgo R-05). Wompi y Meta van con dobles: sin red.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _alta_helpers import inscribir_y_pagar, plan_del_catalogo, suscripcion_de_plan
from _api_helpers import api_headers, uniq
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_SECRETO_DE_EVENTOS = "secreto-de-eventos-de-prueba"
# Monto firmado por Wompi en el webhook: el del link de cada plan.
_CENTAVOS = {"basic": 20_000_000, "premium": 60_000_000}


# ------------------------------------------------------------------ dobles
@pytest.fixture
def vendedores(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Fija la allowlist `PROVISIONING_SELLER_TENANTS` del test."""
    from core.config import get_settings

    def fijar(*valores: Any) -> None:
        monkeypatch.setenv("PROVISIONING_SELLER_TENANTS", ",".join(str(v) for v in valores))
        get_settings.cache_clear()

    yield fijar
    monkeypatch.delenv("PROVISIONING_SELLER_TENANTS", raising=False)
    monkeypatch.delenv("BILLING_WOMPI_EVENTOS_TENANT", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def avisos(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Avisos por WhatsApp (plantilla o texto, ADR-0108) capturados: lo que ve el cliente."""
    from core.gateway.event_notices import NoticeResult

    enviados: list[dict[str, Any]] = []

    async def falso(conversation_id, event_key, context, *, notice_key, default_fallback_text=""):  # type: ignore[no-untyped-def]
        enviados.append({"event": event_key, "context": context, "key": notice_key})
        return NoticeResult("plantilla")

    monkeypatch.setattr("core.gateway.event_notices.send_event_notice", falso)
    return enviados


@pytest.fixture
def correos(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    enviados: list[dict[str, str]] = []

    async def falso(to: str, subject: str, body: str) -> str:
        enviados.append({"to": to, "subject": subject, "body": body})
        return "msg-test"

    monkeypatch.setattr("core.gateway.system_email.send_system_email", falso)
    return enviados


@pytest.fixture
def alertas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Alertas de operación (`send_alert`) y pushes a los dueños, capturados."""
    from core.tasks.push_tasks import notify_owners_push

    enviadas: list[dict[str, str]] = []

    async def alerta(title: str, detail: str, severity: str = "warning") -> None:
        enviadas.append({"title": title, "detail": detail, "severity": severity})

    monkeypatch.setattr("core.shared.alerts.send_alert", alerta)
    monkeypatch.setattr(
        notify_owners_push,
        "delay",
        lambda tenant_id, title, body, data: enviadas.append(
            {"title": title, "detail": body, "severity": "push", "tenant_id": tenant_id}
        ),
    )
    return enviadas


class _Pasarela:
    """Link de la plataforma sin red; la firma del webhook es la real de Wompi."""

    provider_name = "wompi"

    def __init__(self) -> None:
        from core.billing.providers.wompi import WompiBillingProvider

        self.links: list[str] = []
        self._real = WompiBillingProvider(
            public_key="pub", private_key="prv", events_secret=_SECRETO_DE_EVENTOS, sandbox=True
        )

    async def create_payment_link(self, *, reference, amount_cop, expires_in_hours=24, plan_name=""):  # type: ignore[no-untyped-def]
        from core.billing.providers.base import PaymentLink

        link = f"link-{uuid.uuid4().hex[:10]}"
        self.links.append(link)
        return PaymentLink(provider_reference=link, url=f"https://checkout.test/{link}")

    def verify_webhook_signature(self, payload):  # type: ignore[no-untyped-def]
        return self._real.verify_webhook_signature(payload)

    def parse_webhook_event(self, payload):  # type: ignore[no-untyped-def]
        return self._real.parse_webhook_event(payload)


@pytest.fixture
def pasarela(monkeypatch: pytest.MonkeyPatch) -> _Pasarela:
    import core.billing.checkout as checkout
    import core.billing.providers as proveedores

    falsa = _Pasarela()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: falsa)
    monkeypatch.setattr(proveedores, "get_billing_provider", lambda: falsa)
    return falsa


class _WompiDelVendedor:
    """El link del pack de pagos del vendedor, sin red."""

    provider_name = "simulado"

    async def create_payment_link(self, *, reference, amount, currency, expires_in_hours):  # type: ignore[no-untyped-def]
        from pack_payments.providers.base import PaymentLinkResult

        return PaymentLinkResult(
            provider_reference=f"sim-{reference}", payment_link_url=f"https://pagos.simulados/{reference}"
        )


@pytest.fixture
def links_del_vendedor(monkeypatch: pytest.MonkeyPatch) -> None:
    import pack_payments.payments as capacidad
    from pack_payments.payments import WompiPaymentCapability

    from core.payments import payment_registry

    monkeypatch.setattr(capacidad, "build_provider", lambda _config: _WompiDelVendedor())
    payment_registry.register("pack_payments", WompiPaymentCapability())


# ------------------------------------------------------------------ escenario
@dataclass
class Escenario:
    vendedor: uuid.UUID
    negocio: uuid.UUID
    slug: str
    subscription_id: uuid.UUID
    cliente: uuid.UUID
    referencia: str


async def _vendedor(*, con_pagos: bool = False) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("vendedor")
    tid, _ = await onboard_tenant(
        name=f"Vendedor {slug}", slug=slug, packs=["pack_commerce", "pack_subscriptions"], plan="empresarial"
    )
    if con_pagos:
        from core.admin.views import set_pack_for_tenant
        from core.plugin_manager import plugin_manager

        async with tenant_scope(tid):
            await plugin_manager.save_config(
                "pack_payments",
                {
                    "public_key": "pub_test_x",
                    "private_key": "prv_test_x",
                    "events_secret": _SECRETO_DE_EVENTOS,
                    "sandbox": True,
                },
            )
            assert await set_pack_for_tenant("pack_payments", True) is None
    return tid


async def _cliente_con_whatsapp(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:+57{uuid.uuid4().int % 10**10}", name="Ana"
            )
            s.add(cliente)
            await s.flush()
            s.add(Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp"))
            return cliente.id


async def _variante(tid: uuid.UUID, plan: str, precio: int) -> uuid.UUID:
    """Plan del catálogo de `pack_subscriptions` que vende el plan `plan` de la plataforma."""
    return await plan_del_catalogo(tid, plan=plan, precio=precio)


async def _pedido(
    tid: uuid.UUID, plan_id: uuid.UUID, precio: int, cliente: uuid.UUID, datos_alta: dict[str, Any]
) -> str:
    """Inscripción activa en el plan; activarla emite `subscription.activated`. Devuelve su referencia."""
    referencia, _ = await suscripcion_de_plan(
        tid, plan_id=plan_id, precio=precio, customer_id=cliente, datos_alta=datos_alta
    )
    return referencia


async def _inscribir_y_pagar(esc: Escenario, plan_id: uuid.UUID) -> None:
    await inscribir_y_pagar(esc.vendedor, esc.cliente, esc.subscription_id, plan_id)


async def _eventos(tid: uuid.UUID, topic: str) -> list[Any]:
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
                        .order_by(DomainEvent.created_at)
                    )
                ).scalars().all()
            )


async def _entregar(tid: uuid.UUID, topic: str, handler: Callable[..., Any], **filtro: Any) -> int:
    """Entrega los eventos del topic al handler como el despachador (mismo `tenant_scope`)."""
    from core.events.outbox import payload_para_handlers
    from core.shared.tenant import tenant_scope

    entregados = 0
    for evento in await _eventos(tid, topic):
        if any(evento.payload.get(k) != v for k, v in filtro.items()):
            continue
        async with tenant_scope(tid):
            await handler(**payload_para_handlers(evento.payload))
        entregados += 1
    return entregados


async def _negocio_aprovisionado(
    vendedores: Callable[..., None],
    *,
    plan: str = "premium",
    precio: int = 600_000,
    vertical: str = "restaurante",
    con_pagos: bool = False,
    vendedor: uuid.UUID | None = None,
) -> Escenario:
    """El alta automática de HU-SH-03: pedido pagado → `subscription.activated` → negocio."""
    from core.db.models import ProvisioningRequest
    from core.db.session import get_session
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    vendedor = vendedor or await _vendedor(con_pagos=con_pagos)
    vendedores(vendedor)
    cliente = await _cliente_con_whatsapp(vendedor)
    variante = await _variante(vendedor, plan, precio)
    datos = {"email": f"{uniq('owner')}@correo-prueba.co", "telefono": "+573000000000", "negocio": "Acme", "vertical": vertical}
    referencia = await _pedido(vendedor, variante, precio, cliente, datos)
    await _entregar(vendedor, "subscription.activated", on_subscription_activated, referencia=referencia)
    async with tenant_scope(vendedor):
        async with get_session() as s:
            registro = (
                await s.execute(select(ProvisioningRequest).where(ProvisioningRequest.referencia == referencia))
            ).scalar_one()
    assert registro.estado == "completado", registro.motivo
    assert registro.negocio_slug and registro.negocio_slug.startswith("acme-")
    return Escenario(
        vendedor=vendedor,
        negocio=registro.negocio_tenant_id,
        slug=registro.negocio_slug,
        subscription_id=registro.subscription_id,
        cliente=cliente,
        referencia=referencia,
    )


async def _suscripcion_de(tid: uuid.UUID) -> Any:
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return (await s.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == tid))).scalar_one()


async def _suscripcion_del_vendedor(esc: Escenario) -> Any:
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            return await s.get(Subscription, esc.subscription_id)


async def _pagos(tid: uuid.UUID) -> list[Any]:
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(
                        select(SubscriptionPayment)
                        .where(SubscriptionPayment.tenant_id == tid)
                        .order_by(SubscriptionPayment.created_at)
                    )
                ).scalars().all()
            )


async def _cargo(esc: Escenario, periodo: str) -> Any:
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            return (
                await s.execute(
                    select(SubscriptionCharge).where(
                        SubscriptionCharge.tenant_id == esc.vendedor,
                        SubscriptionCharge.subscription_id == esc.subscription_id,
                        SubscriptionCharge.periodo == periodo,
                    )
                )
            ).scalar_one_or_none()


async def _auditoria(tid: uuid.UUID, accion: str) -> list[Any]:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == accion))
                ).scalars().all()
            )


def _mes_antes(fin: datetime) -> datetime:
    """El inicio del período mensual que termina en `fin` (por calendario)."""
    import calendar

    anio, mes = (fin.year, fin.month - 1) if fin.month > 1 else (fin.year - 1, 12)
    return fin.replace(year=anio, month=mes, day=min(fin.day, calendar.monthrange(anio, mes)[1]))


async def _acercar_el_vencimiento(esc: Escenario, *, fin: datetime) -> datetime:
    """Viaje en el tiempo: en los DOS lados (negocio y vendedor) el período en curso pasa a
    terminar en `fin`, y el pago del primer período (el del alta) se mueve con él."""
    from pack_subscriptions.models import Subscription

    from core.db.models import SubscriptionPayment, TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fin = fin.replace(second=0, microsecond=0)
    inicio = _mes_antes(fin)
    async with tenant_scope(esc.negocio):
        async with get_session() as s:
            sub = (
                await s.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == esc.negocio))
            ).scalar_one()
            sub.period_start = inicio
            sub.period_end = fin
            sub.dia_ancla = fin.day
            for pago in (await s.execute(select(SubscriptionPayment))).scalars().all():
                if pago.status == "aprobado":
                    pago.periodo = pago.provider_reference = _referencia(esc, inicio)
                    pago.period_start, pago.period_end = inicio, fin
    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            sub_v = await s.get(Subscription, esc.subscription_id)
            sub_v.periodo_actual_inicio = inicio
            sub_v.proximo_cargo = fin
            sub_v.dia_ancla = fin.day
    return fin


async def _cobrar_periodo(esc: Escenario, inicio: datetime) -> dict[str, Any]:
    """El cron `generar_cargos` del vendedor, solo para ESTA suscripción: genera el cargo del
    período que empieza en `inicio`, con su link y su aviso (si le corresponde)."""
    from pack_subscriptions.capability import _config
    from pack_subscriptions.jobs import generar_cargo_de
    from pack_subscriptions.models import Subscription
    from pack_subscriptions.periodos import etiqueta_periodo

    from core.agents.spec import enabled_packs, load_active_spec
    from core.db.session import get_session
    from core.payments import payment_registry
    from core.shared.tenant import tenant_scope

    periodo = f"{inicio:%Y-%m}"
    if await _cargo(esc, periodo) is not None:
        return {"estado": "ya_existia", "periodo": periodo}
    async with tenant_scope(esc.vendedor):
        cap = payment_registry.capability_for(enabled_packs(await load_active_spec(esc.vendedor)))
        config = await _config()
        async with get_session() as s:
            sub = await s.get(Subscription, esc.subscription_id)
            assert etiqueta_periodo(sub.proximo_cargo, sub.periodicidad) == periodo
            cargo = await generar_cargo_de(s, esc.vendedor, sub, datetime.now(UTC), cap=cap, config=config)
    assert cargo is not None
    return {"estado": "generado", "periodo": periodo}


async def _pagar_por_el_asistente(esc: Escenario, periodo: str, *, provider_reference: str | None = None) -> Any:
    """El cliente paga el link del cargo: `payment.confirmed` llega al pack de suscripciones."""
    from pack_subscriptions.module import SubscriptionsModule

    from core.shared.tenant import tenant_scope

    cargo = await _cargo(esc, periodo)
    assert cargo is not None
    async with tenant_scope(esc.vendedor):
        await SubscriptionsModule()._on_payment_confirmed(
            f"sub-{cargo.id}", "wompi", provider_reference or f"pl-{uuid.uuid4().hex[:8]}", float(cargo.monto)
        )
    return cargo


async def _entregar_cargos_pagados(esc: Escenario) -> int:
    """Los `subscription.charge_paid` de ESTA suscripción (el vendedor puede tener varias)."""
    from core.billing.canales import on_subscription_charge_paid

    return await _entregar(
        esc.vendedor,
        "subscription.charge_paid",
        on_subscription_charge_paid,
        subscription_id=str(esc.subscription_id),
    )


async def _link_del_panel(negocio: uuid.UUID, plan: str = "premium") -> str | None:
    from core.billing.checkout import start_subscription
    from core.shared.tenant import tenant_scope

    async with tenant_scope(negocio):
        return await start_subscription(plan)


async def _pagar_por_el_panel(negocio: uuid.UUID, pasarela: _Pasarela, plan: str = "premium") -> Any:
    from core.billing.checkout import apply_payment

    assert await _link_del_panel(negocio, plan)
    link = pasarela.links[-1]
    assert await apply_payment(provider_reference=link, status="aprobado", amount_in_cents=_CENTAVOS[plan]) is True
    [pago] = [p for p in await _pagos(negocio) if p.provider_reference == link]
    return pago


async def _sincronizar(negocio: uuid.UUID) -> int:
    from core.billing.canales import on_platform_period_paid

    return await _entregar(negocio, "platform.period_paid", on_platform_period_paid)


def _referencia(esc: Escenario, inicio: datetime) -> str:
    return f"plat-{esc.slug}-{inicio:%Y%m}"


# ------------------------------------------------------------------ Gherkin
async def test_pago_por_el_asistente_extiende_el_periodo_del_negocio(vendedores, correos, alertas) -> None:
    """Gherkin «Pago por el asistente»."""
    from core.subscriptions.periodos import sumar_periodo

    esc = await _negocio_aprovisionado(vendedores)
    negocio, sub_v = await _suscripcion_de(esc.negocio), await _suscripcion_del_vendedor(esc)
    # Fuente única alineada: los dos lados cortan el período el mismo día.
    assert negocio.period_end == sub_v.proximo_cargo
    inicio = negocio.period_end
    referencia = _referencia(esc, inicio)

    cargo = await _cobrar_periodo(esc, inicio)
    assert (cargo["estado"], cargo["periodo"]) == ("generado", f"{inicio:%Y-%m}")
    await _pagar_por_el_asistente(esc, cargo["periodo"])
    [evento] = await _eventos(esc.vendedor, "subscription.charge_paid")
    assert evento.payload["periodo_inicio"] == inicio.isoformat()
    assert await _entregar_cargos_pagados(esc) == 1

    negocio = await _suscripcion_de(esc.negocio)
    assert (negocio.plan_id, negocio.status) == ("premium", "active")
    assert negocio.period_end == sumar_periodo(inicio, "mensual", negocio.dia_ancla)
    assert negocio.period_end == (await _suscripcion_del_vendedor(esc)).proximo_cargo
    [pago] = [p for p in await _pagos(esc.negocio) if p.periodo == referencia]
    assert (pago.status, pago.provider, int(pago.amount_cop)) == ("aprobado", "asistente", 600_000)
    assert len(await _auditoria(esc.vendedor, "cobro_plataforma_aplicado")) == 1
    [en_el_negocio] = await _auditoria(esc.negocio, "cobro_plataforma_aplicado")
    assert en_el_negocio.input["referencia"] == referencia

    # Idempotente: el outbox entrega de nuevo (at-least-once) y nada se mueve.
    assert await _entregar_cargos_pagados(esc) == 1
    otra_vez = await _suscripcion_de(esc.negocio)
    assert otra_vez.period_end == negocio.period_end
    assert len([p for p in await _pagos(esc.negocio) if p.periodo == referencia]) == 1
    assert not [a for a in alertas if a["severity"] == "critical"]


async def test_pago_por_el_panel_deja_pagado_el_cargo_del_asistente(
    vendedores, correos, alertas, pasarela, app_client
) -> None:
    """Gherkin «Pago por el panel»: sincronización inversa; el asistente no lo vuelve a cobrar."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.jobs import _avisos_de_cobro

    from core.billing.checkout import apply_payment
    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError
    from core.subscriptions.periodos import sumar_periodo

    esc = await _negocio_aprovisionado(vendedores)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=4))
    referencia = _referencia(esc, inicio)
    cargo = await _cobrar_periodo(esc, inicio)  # el asistente ya mandó su link
    assert cargo["estado"] == "generado"

    # Desde /plan: el período que toca, con su referencia, y el link de la plataforma.
    headers = await api_headers(app_client, esc.negocio)
    plan = (await app_client.get("/api/v1/admin/plan", headers=headers)).json()
    assert (plan["cobro"]["referencia"], plan["cobro"]["estado"]) == (referencia, "pendiente")
    r = await app_client.post("/api/v1/admin/plan/subscribe", json={"plan_id": "premium"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"].endswith(pasarela.links[-1])
    # Pedirlo otra vez no crea un segundo link vivo para el mismo período.
    otra = await app_client.post("/api/v1/admin/plan/subscribe", json={"plan_id": "premium"}, headers=headers)
    assert otra.json()["checkout_url"] == r.json()["checkout_url"] and len(pasarela.links) == 1
    assert await apply_payment(provider_reference=pasarela.links[-1], status="aprobado", amount_in_cents=_CENTAVOS["premium"]) is True
    [pago] = [p for p in await _pagos(esc.negocio) if p.provider_reference == pasarela.links[-1]]
    assert (pago.periodo, pago.status, pago.provider) == (referencia, "aprobado", "wompi")
    negocio = await _suscripcion_de(esc.negocio)
    assert negocio.period_end == sumar_periodo(inicio, "mensual", inicio.day)
    assert await _sincronizar(esc.negocio) == 1

    del_asistente = await _cargo(esc, f"{inicio:%Y-%m}")
    assert del_asistente.estado == "pagado"
    assert (del_asistente.pago_externo["canal"], del_asistente.pago_externo["referencia"]) == ("panel", referencia)
    assert (await _suscripcion_del_vendedor(esc)).proximo_cargo == negocio.period_end
    [sincronizado] = await _auditoria(esc.vendedor, "cobro_plataforma_sincronizado")
    assert sincronizado.output["estado"] == "registrado"

    # El asistente no lo vuelve a cobrar: «ya pagado»…
    async with tenant_scope(esc.vendedor):
        with pytest.raises(SubscriptionError) as exc:
            await SubscriptionCapabilityImpl().charge_link(esc.subscription_id)
    assert exc.value.code == "ya_pagado"
    # …ni lo recuerda el día del vencimiento.
    await _avisos_de_cobro(inicio + timedelta(hours=1))
    recordatorios = await _eventos(esc.vendedor, "subscription.charge_reminder")
    assert not [e for e in recordatorios if e.payload.get("charge_id") == str(del_asistente.id)]


async def test_sin_doble_cobro_el_panel_responde_ya_pagado(vendedores, correos, alertas, pasarela, app_client) -> None:
    """Gherkin «Sin doble cobro»: pagado por el asistente, el panel no genera otro link."""
    from core.billing.checkout import PeriodoYaPagado

    esc = await _negocio_aprovisionado(vendedores)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=4))
    referencia = _referencia(esc, inicio)
    await _cobrar_periodo(esc, inicio)
    await _pagar_por_el_asistente(esc, f"{inicio:%Y-%m}")

    # El evento todavía no llegó al negocio: el panel lo ve igual en el asistente.
    with pytest.raises(PeriodoYaPagado) as exc:
        await _link_del_panel(esc.negocio)
    assert (exc.value.referencia, exc.value.canal) == (referencia, "asistente")

    await _entregar_cargos_pagados(esc)
    with pytest.raises(PeriodoYaPagado) as exc:
        await _link_del_panel(esc.negocio)
    assert (exc.value.referencia, exc.value.canal) == (referencia, "asistente")
    assert pasarela.links == []

    # Por la API del panel: 409 con el código estable.
    headers = await api_headers(app_client, esc.negocio)
    r = await app_client.post("/api/v1/admin/plan/subscribe", json={"plan_id": "premium"}, headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "ya_pagado"
    assert referencia in r.json()["error"]["message"]
    plan = (await app_client.get("/api/v1/admin/plan", headers=headers)).json()
    assert plan["cobro"]["referencia"] == referencia
    assert (plan["cobro"]["estado"], plan["cobro"]["pagado_por"]) == ("pagado", "asistente")
    assert plan["plan_por_el_asistente"] is True


async def test_sin_doble_cobro_el_asistente_responde_ya_pagado(vendedores, correos, alertas, pasarela) -> None:
    """Gherkin «Sin doble cobro», al revés: pagado por el panel, el asistente responde «ya pagado»."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    esc = await _negocio_aprovisionado(vendedores)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=3))
    await _pagar_por_el_panel(esc.negocio, pasarela)
    await _sincronizar(esc.negocio)

    async with tenant_scope(esc.vendedor):
        with pytest.raises(SubscriptionError) as exc:
            await SubscriptionCapabilityImpl().charge_link(esc.subscription_id)
    assert exc.value.code == "ya_pagado"
    # El cargo existe y está pagado por el panel aunque el asistente nunca lo había generado.
    cargo = await _cargo(esc, f"{inicio:%Y-%m}")
    assert (cargo.estado, cargo.pago_externo["canal"]) == ("pagado", "panel")
    # Y la renovación del asistente no lo vuelve a generar.
    assert (await _cobrar_periodo(esc, inicio))["estado"] == "ya_existia"


async def test_pago_doble_se_registra_una_vez_y_se_alerta(vendedores, correos, alertas, pasarela) -> None:
    """Si el cliente igual paga los dos links (el del asistente era viejo), la plata quedó dos
    veces: el período se aplica una sola vez, el segundo pago queda `duplicado` y se alerta."""
    from core.subscriptions.periodos import sumar_periodo

    esc = await _negocio_aprovisionado(vendedores)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=4))
    referencia = _referencia(esc, inicio)
    await _cobrar_periodo(esc, inicio)
    await _pagar_por_el_panel(esc.negocio, pasarela)
    await _sincronizar(esc.negocio)
    # El link viejo del asistente también se paga.
    await _pagar_por_el_asistente(esc, f"{inicio:%Y-%m}")
    eventos = await _eventos(esc.vendedor, "subscription.charge_paid")
    assert [e.payload["pago_duplicado"] for e in eventos] == [True]
    await _entregar_cargos_pagados(esc)

    negocio = await _suscripcion_de(esc.negocio)
    assert negocio.period_end == sumar_periodo(inicio, "mensual", inicio.day)  # extendido UNA vez
    del_periodo = [p for p in await _pagos(esc.negocio) if p.periodo == referencia]
    assert sorted((p.status, p.provider) for p in del_periodo) == [("aprobado", "wompi"), ("duplicado", "asistente")]
    [criticas] = [a for a in alertas if a["severity"] == "critical"]
    assert referencia in criticas["detail"]
    assert len(await _auditoria(esc.negocio, "cobro_plataforma_pago_duplicado")) == 1
    assert len(await _auditoria(esc.vendedor, "subs_pago_sin_aplicar")) == 1
    # Un reintento del evento no vuelve a alertar.
    await _entregar_cargos_pagados(esc)
    assert len([a for a in alertas if a["severity"] == "critical"]) == 1


async def test_renovacion_genera_el_cobro_y_avisa_por_el_asistente(
    vendedores, correos, alertas, avisos, links_del_vendedor
) -> None:
    """Gherkin «Renovación», canal asistente: cobro del siguiente con su referencia y aviso por
    WhatsApp (plantilla fuera de 24 h, ADR-0108)."""
    from core.billing.renovacion import on_platform_renewal_due, renovar_suscripciones
    from core.events.core_handlers import on_subscription_charge_created

    esc = await _negocio_aprovisionado(vendedores, con_pagos=True)
    ahora = datetime.now(UTC)
    inicio = await _acercar_el_vencimiento(esc, fin=ahora + timedelta(days=5) - timedelta(hours=1))
    referencia = _referencia(esc, inicio)

    await renovar_suscripciones(ahora)
    [cobro] = [p for p in await _pagos(esc.negocio) if p.periodo == referencia]
    assert (cobro.status, cobro.provider, cobro.provider_reference) == ("pendiente", "plataforma", referencia)
    assert cobro.period_start == inicio and int(cobro.amount_cop) == 600_000
    [evento] = await _eventos(esc.negocio, "platform.renewal_due")
    assert evento.payload["referencia"] == referencia

    await _entregar(esc.negocio, "platform.renewal_due", on_platform_renewal_due)
    [avisada] = await _auditoria(esc.negocio, "renovacion_avisada")
    assert (avisada.output["canal"], avisada.output["suscripcion_en_el_asistente"]) == ("asistente", "activa")
    assert not [c for c in correos if "vence" in c["subject"]]  # el canal preferido es el asistente

    # El cargo con su link y su aviso los manda el cron del vendedor (dias_anticipacion = 5).
    await _cobrar_periodo(esc, inicio)
    cargo = await _cargo(esc, f"{inicio:%Y-%m}")
    assert cargo is not None and cargo.estado == "pendiente" and cargo.link_url
    await _entregar(esc.vendedor, "subscription.charge_created", on_subscription_charge_created, charge_id=str(cargo.id))
    [aviso] = [a for a in avisos if a["event"] == "subscription.charge_created"]
    assert aviso["context"]["cargo"]["link"] == cargo.link_url
    assert aviso["context"]["cargo"]["periodo"] == f"{inicio:%Y-%m}"

    # Idempotente: el job y el evento otra vez no generan otro cobro ni otro cargo.
    await renovar_suscripciones(ahora)
    await _entregar(esc.negocio, "platform.renewal_due", on_platform_renewal_due)
    assert len([p for p in await _pagos(esc.negocio) if p.periodo == referencia]) == 1
    assert len(await _eventos(esc.negocio, "platform.renewal_due")) == 1


async def test_renovacion_por_el_panel_avisa_por_correo(vendedores, correos, alertas) -> None:
    """Gherkin «Renovación», canal panel: un negocio sin canal asistente recibe el correo con
    la referencia del período y el enlace a /plan."""
    from core.alta_negocio import onboard_tenant
    from core.billing.renovacion import on_platform_renewal_due, renovar_negocio
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedores()  # sin vendedores: ningún negocio tiene canal asistente
    email = f"{uniq('duena')}@correo-prueba.co"
    slug = uniq("panel")
    negocio, _ = await onboard_tenant(
        name="Negocio de panel", slug=slug, plan="basic", admin_email=email, admin_password="clave-segura-1"
    )
    ahora = datetime.now(UTC)
    fin = (ahora + timedelta(days=5) - timedelta(minutes=5)).replace(second=0, microsecond=0)
    async with tenant_scope(negocio):
        async with get_session() as s:
            sub = (await s.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == negocio))).scalar_one()
            sub.period_end = fin
            sub.provider = "wompi"

    referencia = await renovar_negocio(negocio, now=ahora, dias=5)
    assert referencia == f"plat-{slug}-{fin:%Y%m}"
    assert await renovar_negocio(negocio, now=ahora, dias=5) is None  # un cobro por período
    await _entregar(negocio, "platform.renewal_due", on_platform_renewal_due)
    [correo] = correos
    assert correo["to"] == email
    assert referencia in correo["body"] and "/plan" in correo["body"]
    assert "$200.000" in correo["body"]
    assert any(a["severity"] == "push" and a["tenant_id"] == str(negocio) for a in alertas)
    [avisada] = await _auditoria(negocio, "renovacion_avisada")
    assert (avisada.output["canal"], avisada.output["correos"]) == ("panel", 1)
    assert avisada.output["remitente"] == "plataforma"  # sin vendedor: SMTP de la plataforma


async def test_renovacion_no_cobra_un_periodo_ya_pagado(vendedores, correos, alertas, pasarela) -> None:
    from core.billing.renovacion import renovar_negocio

    esc = await _negocio_aprovisionado(vendedores)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=3))
    await _pagar_por_el_panel(esc.negocio, pasarela)  # pagado antes de que corra el job
    assert await renovar_negocio(esc.negocio, now=datetime.now(UTC), dias=5) is None
    assert [p.status for p in await _pagos(esc.negocio) if p.periodo == _referencia(esc, inicio)] == ["aprobado"]


async def test_conciliacion_alerta_las_diferencias_con_el_detalle(vendedores, correos, alertas, pasarela) -> None:
    """Gherkin «Conciliación»: el job diario alerta cada diferencia entre los dos lados."""
    from core.billing.conciliacion import conciliar_vendedor

    vendedor = await _vendedor()
    a = await _negocio_aprovisionado(vendedores, vendedor=vendedor)
    b = await _negocio_aprovisionado(vendedores, vendedor=vendedor)
    al_dia = await _negocio_aprovisionado(vendedores, vendedor=vendedor)
    ahora = datetime.now(UTC)
    # A: el asistente cobró el período, pero el evento nunca llegó al negocio.
    inicio_a = await _acercar_el_vencimiento(a, fin=ahora + timedelta(days=4))
    await _cobrar_periodo(a, inicio_a)
    await _pagar_por_el_asistente(a, f"{inicio_a:%Y-%m}")
    # B: pagó por el panel, pero la sincronización inversa no corrió: el asistente lo cobraría.
    inicio_b = await _acercar_el_vencimiento(b, fin=ahora + timedelta(days=4))
    await _cobrar_periodo(b, inicio_b)
    await _pagar_por_el_panel(b.negocio, pasarela)
    # Al día: el período lo pagó el asistente y llegó al negocio.
    inicio_c = await _acercar_el_vencimiento(al_dia, fin=ahora + timedelta(days=4))
    await _cobrar_periodo(al_dia, inicio_c)
    await _pagar_por_el_asistente(al_dia, f"{inicio_c:%Y-%m}")
    await _entregar_cargos_pagados(al_dia)

    negocios, diferencias = await conciliar_vendedor(vendedor, now=ahora)
    assert negocios == 3
    por_tipo = {(d.tipo, d.negocio): d for d in diferencias}
    sin_aplicar = por_tipo[("pagado_en_el_asistente_sin_aplicar", a.slug)]
    assert sin_aplicar.referencia == _referencia(a, inicio_a)
    sin_sincronizar = por_tipo[("pagado_en_el_panel_sin_sincronizar", b.slug)]
    assert sin_sincronizar.referencia == _referencia(b, inicio_b)
    assert sin_sincronizar.detalle["pagado_por"] == "wompi"
    assert not [d for d in diferencias if d.negocio == al_dia.slug]

    [auditada] = await _auditoria(vendedor, "conciliacion_cobro_plataforma")
    assert auditada.output["total"] == len(diferencias)
    tipos = {d["tipo"] for d in auditada.output["diferencias"]}
    assert {"pagado_en_el_asistente_sin_aplicar", "pagado_en_el_panel_sin_sincronizar"} <= tipos
    [alerta] = [x for x in alertas if x["severity"] == "warning"]
    assert a.slug in alerta["detail"] and b.slug in alerta["detail"]

    # Corregido el lado que faltaba, la diferencia desaparece.
    await _entregar_cargos_pagados(a)
    await _sincronizar(b.negocio)
    _, despues = await conciliar_vendedor(vendedor, now=ahora)
    assert not [d for d in despues if d.negocio in (a.slug, b.slug, al_dia.slug)]


async def test_plan_del_alta_entra_en_la_renovacion_y_se_desbloquea_al_pagar(
    vendedores, correos, alertas, avisos, links_del_vendedor
) -> None:
    """Gherkin «Plan creado por el alta»: vence, se renueva como cualquier otro y paga."""
    from core.billing.enforcement import is_tenant_operational
    from core.billing.renovacion import on_platform_renewal_due, renovar_suscripciones
    from core.tasks.billing_lifecycle import normalize_expired_subscriptions

    esc = await _negocio_aprovisionado(vendedores, con_pagos=True)
    ahora = datetime.now(UTC)
    vencio = await _acercar_el_vencimiento(esc, fin=ahora - timedelta(days=2))
    await normalize_expired_subscriptions()
    assert (await _suscripcion_de(esc.negocio)).status == "past_due"
    assert await is_tenant_operational(esc.negocio) is False  # bloqueo duro (ADR-0045)

    await renovar_suscripciones(ahora)
    referencia = _referencia(esc, vencio)
    assert [p.status for p in await _pagos(esc.negocio) if p.periodo == referencia] == ["pendiente"]
    await _entregar(esc.negocio, "platform.renewal_due", on_platform_renewal_due)
    await _cobrar_periodo(esc, vencio)  # el cron del vendedor
    cargo = await _cargo(esc, f"{vencio:%Y-%m}")
    assert cargo is not None and cargo.estado == "pendiente"

    await _pagar_por_el_asistente(esc, cargo.periodo)
    await _entregar_cargos_pagados(esc)
    negocio = await _suscripcion_de(esc.negocio)
    assert negocio.status == "active" and negocio.period_end > ahora + timedelta(days=25)
    assert await is_tenant_operational(esc.negocio) is True
    assert [p.status for p in await _pagos(esc.negocio) if p.periodo == referencia] == ["aprobado"]


async def test_plan_del_alta_vencido_se_desbloquea_pagando_por_el_panel(vendedores, correos, alertas, pasarela) -> None:
    from core.billing.enforcement import is_tenant_operational
    from core.tasks.billing_lifecycle import normalize_expired_subscriptions

    esc = await _negocio_aprovisionado(vendedores)
    vencio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) - timedelta(days=3))
    await normalize_expired_subscriptions()
    assert await is_tenant_operational(esc.negocio) is False

    pago = await _pagar_por_el_panel(esc.negocio, pasarela)
    assert pago.periodo == _referencia(esc, vencio)  # el período vencido, que ya corre
    assert await is_tenant_operational(esc.negocio) is True
    await _sincronizar(esc.negocio)
    assert (await _suscripcion_del_vendedor(esc)).estado == "activa"
    assert (await _cargo(esc, f"{vencio:%Y-%m}")).estado == "pagado"


async def test_free_que_compra_un_plan_pago_cambia_de_plan_y_aplica_el_preset(vendedores, correos, alertas) -> None:
    """Gherkin «Cambio de plan desde el asistente»: sin otro negocio y con el preset pendiente."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.db.models import ProvisioningRequest, Tenant
    from core.db.session import get_session
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores, plan="free", precio=0)
    antes = await _suscripcion_de(esc.negocio)
    assert (antes.plan_id, antes.status) == ("free", "trialing")
    async with get_session() as s:
        alta = (await s.get(Tenant, esc.negocio)).settings["alta"]
    assert alta["preset"]["estado"] == "pendiente"  # ningún preset cabe en Free

    premium = await _variante(esc.vendedor, "premium", 600_000)
    await _inscribir_y_pagar(esc, premium)
    [evento] = [e for e in await _eventos(esc.vendedor, "subscription.activated") if e.payload["cambio_de_plan"]]
    assert evento.payload["subscription_id"] == str(esc.subscription_id)
    await _entregar(esc.vendedor, "subscription.activated", on_subscription_activated, cambio_de_plan=True)

    negocio = await _suscripcion_de(esc.negocio)
    sub_v = await _suscripcion_del_vendedor(esc)
    assert (negocio.plan_id, negocio.status, negocio.trial_ends_at) == ("premium", "active", None)
    assert (negocio.period_start, negocio.period_end) == (sub_v.periodo_actual_inicio, sub_v.proximo_cargo)
    [pago] = [p for p in await _pagos(esc.negocio) if p.status == "aprobado"]
    assert pago.periodo == _referencia(esc, sub_v.periodo_actual_inicio)

    async with get_session() as s:
        tenant = await s.get(Tenant, esc.negocio)
        vendedor_slug = (await s.get(Tenant, esc.vendedor)).slug
        negocios_del_cliente = (
            await s.execute(
                select(func.count())
                .select_from(Tenant)
                .where(Tenant.settings["alta"]["vendedor"].astext == vendedor_slug)
            )
        ).scalar_one()
    assert tenant.settings["preset"]["id"] == "restaurante"
    assert tenant.settings["alta"]["preset"]["estado"] == "aplicado"
    async with tenant_scope(esc.negocio):
        assert "pack_commerce" in enabled_packs(await load_active_spec(esc.negocio))
    assert negocios_del_cliente == 1
    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            filas = (
                await s.execute(select(ProvisioningRequest).where(ProvisioningRequest.customer_id == esc.cliente))
            ).scalars().all()
    assert [(f.referencia, f.plan_id) for f in filas] == [(esc.referencia, "premium")]
    [cambio] = await _auditoria(esc.vendedor, "aprovisionamiento_cambio_de_plan")
    assert (cambio.output["estado"], cambio.output["plan_anterior"], cambio.output["plan"]) == (
        "periodo_pagado",
        "free",
        "premium",
    )
    assert cambio.output["preset"]["estado"] == "aplicado"
    assert len(await _auditoria(esc.negocio, "plan_cambiado_por_el_asistente")) == 1

    # Idempotente: el mismo evento otra vez no mueve el período ni reaplica el preset.
    await _entregar(esc.vendedor, "subscription.activated", on_subscription_activated, cambio_de_plan=True)
    assert (await _suscripcion_de(esc.negocio)).period_end == negocio.period_end
    assert len([p for p in await _pagos(esc.negocio) if p.status == "aprobado"]) == 1


async def test_free_vencido_que_compra_un_plan_pago_sigue_con_su_negocio(vendedores, correos, alertas) -> None:
    """Después de que venció la prueba: la suscripción Free vencida se reusa (antes nacía otra
    y el alta creaba un segundo negocio)."""
    from pack_subscriptions.jobs import _marcar_vencidas

    from core.billing.enforcement import is_tenant_operational
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores, plan="free", precio=0)
    ahora = datetime.now(UTC)
    # La prueba terminó en los dos lados.
    from pack_subscriptions.models import Subscription

    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            sub_v = await s.get(Subscription, esc.subscription_id)
            sub_v.trial_termina = ahora - timedelta(days=1)
    async with tenant_scope(esc.negocio):
        async with get_session() as s:
            sub = (await s.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == esc.negocio))).scalar_one()
            sub.trial_ends_at = sub.period_end = ahora - timedelta(days=1)
            sub.status = "expired"
    await _marcar_vencidas(ahora)
    assert (await _suscripcion_del_vendedor(esc)).estado == "vencida"
    assert await is_tenant_operational(esc.negocio) is False

    basic = await _variante(esc.vendedor, "basic", 200_000)
    await _inscribir_y_pagar(esc, basic)
    [evento] = [e for e in await _eventos(esc.vendedor, "subscription.activated") if e.payload["cambio_de_plan"]]
    assert evento.payload["subscription_id"] == str(esc.subscription_id)  # la misma suscripción
    await _entregar(esc.vendedor, "subscription.activated", on_subscription_activated, cambio_de_plan=True)
    negocio = await _suscripcion_de(esc.negocio)
    assert (negocio.plan_id, negocio.status) == ("basic", "active")
    assert await is_tenant_operational(esc.negocio) is True


async def test_cambio_de_plan_con_sub_cambiar_plan_cambia_el_plan_del_negocio(vendedores, correos, alertas) -> None:
    """El mismo cambio por la tool `sub_cambiar_plan` (cargo de cambio de plan, sin pedido)."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.module import SubscriptionsModule

    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores, plan="free", precio=0)
    premium = await _variante(esc.vendedor, "premium", 600_000)
    async with tenant_scope(esc.vendedor):
        from pack_subscriptions.planes import obtener, snapshot

        vista = await SubscriptionCapabilityImpl().change_plan(
            esc.subscription_id, plan_id=str(premium), plan=snapshot(await obtener(premium))
        )
    assert vista["cambio"] == "pendiente_pago"
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session

    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            cargo = (
                await s.execute(
                    select(SubscriptionCharge).where(
                        SubscriptionCharge.subscription_id == esc.subscription_id, SubscriptionCharge.tipo == "cambio_plan"
                    )
                )
            ).scalar_one()
        await SubscriptionsModule()._on_payment_confirmed(f"sub-{cargo.id}", "wompi", "pl-cambio", 600_000.0)
    eventos = [e for e in await _eventos(esc.vendedor, "subscription.activated") if e.payload.get("cambio_de_plan")]
    assert eventos and eventos[-1].payload["referencia"] == esc.referencia
    await _entregar(esc.vendedor, "subscription.activated", on_subscription_activated, cambio_de_plan=True)
    negocio = await _suscripcion_de(esc.negocio)
    assert (negocio.plan_id, negocio.status) == ("premium", "active")


# ------------------------------------------------------------------ aislamiento
@pytest.mark.aislamiento
async def test_un_cargo_de_otro_vendedor_no_toca_el_negocio(vendedores, correos, alertas) -> None:
    """El vínculo es la fila del alta DEL vendedor: un evento de otro vendedor (aunque traiga el
    id de la suscripción) no encuentra el negocio y no lo toca."""
    from core.billing.canales import on_subscription_charge_paid
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores)
    otro = await _vendedor()
    vendedores(esc.vendedor, otro)
    antes = await _suscripcion_de(esc.negocio)
    falso = {
        "charge_id": str(uuid.uuid4()),
        "subscription_id": str(esc.subscription_id),
        "periodo": f"{antes.period_end:%Y-%m}",
        "periodo_inicio": antes.period_end.isoformat(),
        "periodo_fin": (antes.period_end + timedelta(days=31)).isoformat(),
        "monto": "600000",
        "plan_id": None,
    }
    async with tenant_scope(otro):
        await on_subscription_charge_paid(**falso)
    # Un negocio fuera de la allowlist tampoco.
    vendedores(otro)
    async with tenant_scope(esc.vendedor):
        await on_subscription_charge_paid(**falso)
    despues = await _suscripcion_de(esc.negocio)
    assert despues.period_end == antes.period_end
    assert [p.periodo for p in await _pagos(esc.negocio)] == [f"plat-{esc.slug}-{antes.period_start:%Y%m}"]


@pytest.mark.aislamiento
async def test_cada_lado_solo_ve_sus_filas(vendedores, correos, alertas, pasarela) -> None:
    """RLS forzada: el negocio no ve los cargos del vendedor ni el vendedor los pagos del negocio,
    y la sincronización inversa solo toca la suscripción del negocio que pagó."""
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor()
    a = await _negocio_aprovisionado(vendedores, vendedor=vendedor)
    b = await _negocio_aprovisionado(vendedores, vendedor=vendedor)
    inicio = await _acercar_el_vencimiento(a, fin=datetime.now(UTC) + timedelta(days=2))
    await _acercar_el_vencimiento(b, fin=inicio)
    await _cobrar_periodo(b, inicio)
    await _pagar_por_el_panel(a.negocio, pasarela)
    await _sincronizar(a.negocio)
    assert (await _cargo(a, f"{inicio:%Y-%m}")).estado == "pagado"
    assert (await _cargo(b, f"{inicio:%Y-%m}")).estado == "pendiente"  # el de B no se toca

    async with tenant_scope(a.negocio):
        async with get_session() as s:
            assert (await s.execute(select(SubscriptionCharge))).scalars().all() == []
            pagos = (await s.execute(select(SubscriptionPayment))).scalars().all()
    assert {p.tenant_id for p in pagos} == {a.negocio}
    async with tenant_scope(vendedor):
        async with get_session() as s:
            assert (await s.execute(select(SubscriptionPayment))).scalars().all() == []


@pytest.mark.aislamiento
async def test_sin_el_vendedor_en_la_allowlist_no_hay_canal_asistente(vendedores, correos, alertas) -> None:
    """Fail-closed: si el vendedor sale de la allowlist, el negocio queda solo con el panel."""
    from core.billing.canales import CANAL_PANEL, canal_preferido, vinculo_asistente

    esc = await _negocio_aprovisionado(vendedores)
    assert (await vinculo_asistente(esc.negocio)).subscription_id == esc.subscription_id
    vendedores()
    assert await vinculo_asistente(esc.negocio) is None
    assert (await canal_preferido(esc.negocio))[0] == CANAL_PANEL


@pytest.mark.aislamiento
async def test_por_el_panel_un_negocio_del_asistente_no_cambia_de_plan(vendedores, correos, alertas, pasarela, app_client) -> None:
    """El plan de un negocio del alta automática cambia por el asistente (la fuente que lo cobra)."""
    esc = await _negocio_aprovisionado(vendedores)
    headers = await api_headers(app_client, esc.negocio)
    r = await app_client.post("/api/v1/admin/plan/subscribe", json={"plan_id": "basic"}, headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "cambio_en_el_asistente"
    assert pasarela.links == []


# ------------------------------------------------------------------ R-05
def _evento_wompi(link_id: str, *, centavos: int, secreto: str = _SECRETO_DE_EVENTOS) -> dict[str, Any]:
    """Evento `transaction.updated` firmado como Wompi (checksum de `signature.properties`)."""
    transaccion = {"id": f"tx-{uuid.uuid4().hex[:8]}", "status": "APPROVED", "amount_in_cents": centavos, "payment_link_id": link_id}
    propiedades = ["transaction.id", "transaction.status", "transaction.amount_in_cents"]
    timestamp = 1_760_000_000
    valores = "".join(str(transaccion[p.split(".")[1]]) for p in propiedades)
    checksum = hashlib.sha256(f"{valores}{timestamp}{secreto}".encode()).hexdigest()
    return {
        "event": "transaction.updated",
        "data": {"transaction": transaccion},
        "environment": "test",
        "signature": {"properties": propiedades, "checksum": checksum},
        "timestamp": timestamp,
    }


@pytest.mark.aislamiento
async def test_r05_un_pago_del_vendedor_que_llega_por_la_url_de_la_plataforma_se_reenvia(
    vendedores, correos, alertas, pasarela, links_del_vendedor, app_client, monkeypatch
) -> None:
    """Riesgo R-05: una sola URL de eventos por cuenta de Wompi. Con la cuenta compartida, el
    webhook de la plataforma le pasa al negocio vendedor lo que no es un pago de la plataforma;
    sin la variable (cuentas separadas) no se reenvía nada."""
    from pack_payments.models import PaymentTransaction
    from pack_payments.payments import WompiPaymentCapability

    from core.config import get_settings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    vendedor = await _vendedor(con_pagos=True)
    vendedores(vendedor)
    async with tenant_scope(vendedor):
        link = await WompiPaymentCapability().create_link(reference=f"OC-{uuid.uuid4().hex[:6]}", amount=250_000)
    async with tenant_scope(vendedor):
        async with get_session() as s:
            transaccion = (
                await s.execute(select(PaymentTransaction).where(PaymentTransaction.reference == link.reference))
            ).scalar_one()
    evento = _evento_wompi(transaccion.provider_reference, centavos=25_000_000)

    # Cuentas separadas (sin la variable): la plataforma no lo reenvía.
    r = await app_client.post("/webhooks/billing/wompi", json=evento)
    assert (r.status_code, r.json()["status"]) == (200, "ignored")

    monkeypatch.setenv("BILLING_WOMPI_EVENTOS_TENANT", str(vendedor))
    get_settings.cache_clear()
    r = await app_client.post("/webhooks/billing/wompi", json=evento)
    assert (r.status_code, r.json()["status"]) == (200, "reenviado")
    async with tenant_scope(vendedor):
        async with get_session() as s:
            transaccion = await s.get(PaymentTransaction, transaccion.id)
    assert transaccion.status == "aprobado"
    [confirmado] = await _eventos(vendedor, "payment.confirmed")
    assert confirmado.payload["reference"] == link.reference

    # Una firma que no es de la cuenta no pasa ni por la plataforma.
    falso = _evento_wompi(transaccion.provider_reference, centavos=25_000_000, secreto="otro-secreto")
    assert (await app_client.post("/webhooks/billing/wompi", json=falso)).status_code == 401


@pytest.mark.aislamiento
async def test_r05_un_pago_de_la_plataforma_sigue_aplicandose_en_su_negocio(
    vendedores, correos, alertas, pasarela, app_client, monkeypatch
) -> None:
    from core.alta_negocio import onboard_tenant
    from core.config import get_settings

    vendedor = await _vendedor()
    monkeypatch.setenv("BILLING_WOMPI_EVENTOS_TENANT", str(vendedor))
    get_settings.cache_clear()
    negocio, _ = await onboard_tenant(name="Negocio R05", slug=uniq("r05"))
    assert await _link_del_panel(negocio, "basic")
    evento = _evento_wompi(pasarela.links[-1], centavos=20_000_000)
    r = await app_client.post("/webhooks/billing/wompi", json=evento)
    assert (r.status_code, r.json()["status"]) == (200, "ok")
    sub = await _suscripcion_de(negocio)
    assert (sub.plan_id, sub.status) == ("basic", "active")
    assert await _eventos(vendedor, "payment.confirmed") == []


# ------------------------------------------------------------------ revisión de aislamiento
@pytest.mark.aislamiento
async def test_un_pago_del_panel_por_otro_monto_no_se_aplica(vendedores, correos, alertas, pasarela, app_client) -> None:
    """El monto firmado del webhook tiene que ser el del link (como en `pack_payments`)."""
    from core.alta_negocio import onboard_tenant

    vendedores()
    negocio, _ = await onboard_tenant(name="Negocio monto", slug=uniq("monto"))
    assert await _link_del_panel(negocio, "basic")
    evento = _evento_wompi(pasarela.links[-1], centavos=20_000_100)
    r = await app_client.post("/webhooks/billing/wompi", json=evento)
    assert r.status_code == 200
    assert (await _suscripcion_de(negocio)).plan_id == "free"
    [pago] = await _pagos(negocio)
    assert pago.status == "monto_distinto"
    assert [a for a in alertas if a["severity"] == "critical" and "monto distinto" in a["title"]]


@pytest.mark.aislamiento
async def test_un_period_paid_sin_pago_aprobado_no_marca_nada_en_el_vendedor(vendedores, correos, alertas) -> None:
    """La sincronización inversa lee el período del pago aprobado del negocio, no del evento."""
    from core.billing.canales import on_platform_period_paid
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=3))
    await _cobrar_periodo(esc, inicio)
    async with tenant_scope(esc.negocio):
        await on_platform_period_paid(
            referencia=_referencia(esc, inicio),
            periodo_inicio=inicio.isoformat(),
            periodo_fin=(inicio + timedelta(days=31)).isoformat(),
            plan_id="premium",
        )
    assert (await _cargo(esc, f"{inicio:%Y-%m}")).estado == "pendiente"


@pytest.mark.aislamiento
async def test_r05_una_configuracion_mala_responde_500_para_que_wompi_reintente(
    vendedores, correos, alertas, pasarela, app_client, monkeypatch
) -> None:
    """Con `BILLING_WOMPI_EVENTOS_TENANT` puesto, un reenvío que no se puede hacer (negocio sin
    pack de pagos, inactivo o UUID inválido) responde 5xx: un 200 perdería el pago."""
    from core.config import get_settings
    from core.tenants import deactivate_tenant

    vendedores()
    sin_pagos = await _vendedor()  # no es vendedor de la allowlist ni tiene pack de pagos
    evento = _evento_wompi("link-ajeno", centavos=100)
    for valor in (str(sin_pagos), "no-es-uuid"):
        monkeypatch.setenv("BILLING_WOMPI_EVENTOS_TENANT", valor)
        get_settings.cache_clear()
        assert (await app_client.post("/webhooks/billing/wompi", json=evento)).status_code == 500
    inactivo = await _vendedor(con_pagos=True)
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as s:
        slug = (await s.get(Tenant, inactivo)).slug
    await deactivate_tenant(slug)
    monkeypatch.setenv("BILLING_WOMPI_EVENTOS_TENANT", str(inactivo))
    get_settings.cache_clear()
    assert (await app_client.post("/webhooks/billing/wompi", json=evento)).status_code == 500


async def test_quien_paga_por_el_panel_no_recibe_el_link_del_asistente(
    vendedores, correos, alertas, pasarela, links_del_vendedor
) -> None:
    """El canal preferido se aplica donde sale el aviso: si el último período lo pagó el panel,
    el cron del vendedor genera el cargo siguiente sin link, sin aviso y sin recordatorios."""
    from pack_subscriptions.jobs import _avisos_de_cobro

    esc = await _negocio_aprovisionado(vendedores, con_pagos=True)
    inicio = await _acercar_el_vencimiento(esc, fin=datetime.now(UTC) + timedelta(days=3))
    await _pagar_por_el_panel(esc.negocio, pasarela)
    await _sincronizar(esc.negocio)
    siguiente = (await _suscripcion_del_vendedor(esc)).proximo_cargo
    assert siguiente > inicio

    await _cobrar_periodo(esc, siguiente)
    cargo = await _cargo(esc, f"{siguiente:%Y-%m}")
    assert (cargo.estado, cargo.link_url) == ("pendiente", None)
    creados = await _eventos(esc.vendedor, "subscription.charge_created")
    assert not [e for e in creados if e.payload.get("charge_id") == str(cargo.id)]
    await _avisos_de_cobro(siguiente + timedelta(hours=1))
    recordatorios = await _eventos(esc.vendedor, "subscription.charge_reminder")
    assert not [e for e in recordatorios if e.payload.get("charge_id") == str(cargo.id)]


async def test_recompra_despues_de_cancelar_va_a_revision(vendedores, correos, alertas) -> None:
    """Quien canceló y vuelve a comprar abre otra suscripción: no se crea un segundo negocio."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.db.models import ProvisioningRequest
    from core.db.session import get_session
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores)
    async with tenant_scope(esc.vendedor):
        await SubscriptionCapabilityImpl().cancel(esc.subscription_id)
    basic = await _variante(esc.vendedor, "basic", 200_000)
    pedido = await _pedido(esc.vendedor, basic, 200_000, esc.cliente, {"email": "otro@correo-prueba.co", "negocio": "Acme 2"})
    await _entregar(esc.vendedor, "subscription.activated", on_subscription_activated, referencia=pedido)
    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            fila = (
                await s.execute(select(ProvisioningRequest).where(ProvisioningRequest.referencia == pedido))
            ).scalar_one()
    assert (fila.estado, fila.negocio_tenant_id) == ("revision", None)
    assert "recompra" in fila.motivo


@pytest.mark.aislamiento
async def test_un_plan_activado_por_menos_que_su_precio_no_se_aplica(vendedores, correos, alertas) -> None:
    """Una suscripción del vendedor activada a mano con otro precio (p. ej. Premium a $1) no
    sube el plan del negocio ni extiende su período: queda `monto_distinto` y se alerta."""
    from pack_subscriptions.capability import _emitir_activada
    from pack_subscriptions.models import Subscription

    from core.billing.canales import on_subscription_charge_paid
    from core.db.session import get_session
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores, plan="free", precio=0)
    premium = await _variante(esc.vendedor, "premium", 600_000)
    # La suscripción del cliente pasa a Premium «a mano» con un valor de período de $1.
    async with tenant_scope(esc.vendedor):
        async with get_session() as s:
            sub = await s.get(Subscription, esc.subscription_id)
            sub.plan_id, sub.valor_periodo, sub.concepto, sub.estado = premium, 1, "Plan Premium", "activa"
            await _emitir_activada(s, sub, cambio=True, charge_id=uuid.uuid4())
    await _entregar(esc.vendedor, "subscription.activated", on_subscription_activated, cambio_de_plan=True)
    negocio = await _suscripcion_de(esc.negocio)
    assert (negocio.plan_id, negocio.status) == ("free", "trialing")
    assert [p.status for p in await _pagos(esc.negocio)] == ["monto_distinto"]
    assert [a for a in alertas if a["severity"] == "critical" and "menos que su precio" in a["title"]]

    # Un cargo de período por $1 tampoco extiende el plan.
    antes = await _suscripcion_de(esc.negocio)
    async with tenant_scope(esc.vendedor):
        await on_subscription_charge_paid(
            charge_id=str(uuid.uuid4()),
            subscription_id=str(esc.subscription_id),
            periodo=f"{antes.period_end:%Y-%m}",
            periodo_inicio=antes.period_end.isoformat(),
            periodo_fin=(antes.period_end + timedelta(days=31)).isoformat(),
            monto="1",
            plan_id=str(premium),
        )
    assert (await _suscripcion_de(esc.negocio)).period_end == antes.period_end


async def _renovacion_por_el_panel_de_un_negocio_del_alta(
    vendedores: Callable[..., None],
) -> Escenario:
    """Negocio del alta automática que paga por el panel, con el período venciendo en 5 días."""
    from core.admin.invitations_service import aceptar_invitacion
    from core.billing.renovacion import renovar_negocio
    from core.db.models import TenantInvitation, TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc = await _negocio_aprovisionado(vendedores, con_pagos=True)
    # El aviso va a los dueños miembros: el dueño acepta la invitación del alta.
    async with get_session() as s:
        inv = (
            (
                await s.execute(
                    select(TenantInvitation).where(TenantInvitation.tenant_id == esc.negocio)
                )
            )
            .scalars()
            .first()
        )
    assert inv is not None
    assert await aceptar_invitacion(invitation_id=inv.id, admin_id=inv.admin_id) is None
    ahora = datetime.now(UTC)
    await _acercar_el_vencimiento(esc, fin=ahora + timedelta(days=5) - timedelta(hours=1))
    async with tenant_scope(esc.negocio):
        async with get_session() as s:
            sub = (
                await s.execute(
                    select(TenantSubscription).where(TenantSubscription.tenant_id == esc.negocio)
                )
            ).scalar_one()
            sub.provider = "wompi"  # pagó el último período por el panel
    assert await renovar_negocio(esc.negocio, now=ahora, dias=5)
    return esc


async def test_renovacion_por_el_panel_sale_por_el_smtp_del_vendedor(
    vendedores, correos, alertas, monkeypatch
) -> None:
    """Enmienda de ADR-0123 (2026-10-09): el aviso de renovación es comunicación del vendedor.
    Con integración SMTP activa en el vendedor, sale por ella (en el tenant del vendedor) y no
    por el SMTP de la plataforma."""
    from core.billing.renovacion import on_platform_renewal_due
    from core.shared.tenant import get_current_tenant_id

    encolados: list[dict[str, Any]] = []

    async def cola_falsa(to: str, subject: str, body: str) -> str:
        encolados.append({"tenant": get_current_tenant_id(), "to": to, "body": body})
        return "msg-vendedor"

    monkeypatch.setattr("core.gateway.outbound_email.queue_outbound_email", cola_falsa)
    esc = await _renovacion_por_el_panel_de_un_negocio_del_alta(vendedores)
    await _entregar(esc.negocio, "platform.renewal_due", on_platform_renewal_due)

    assert encolados and all(e["tenant"] == esc.vendedor for e in encolados)
    assert all("/plan" in e["body"] for e in encolados)
    # El SMTP de la plataforma no mandó el aviso de renovación (el acceso del alta sí sale por él).
    assert not [c for c in correos if "vence" in c["subject"]]
    [avisada] = await _auditoria(esc.negocio, "renovacion_avisada")
    assert (avisada.output["canal"], avisada.output["remitente"]) == ("panel", "vendedor")


async def test_renovacion_por_el_panel_sin_smtp_del_vendedor_usa_el_de_la_plataforma(
    vendedores, correos, alertas, monkeypatch
) -> None:
    """Si el vendedor no tiene una integración de correo activa, el aviso sale por el SMTP de la
    plataforma (respaldo)."""
    from core.billing.renovacion import on_platform_renewal_due
    from core.gateway.outbound_email import EmailOutboxError

    async def sin_conexion(to: str, subject: str, body: str) -> str:
        raise EmailOutboxError("no_connection")

    monkeypatch.setattr("core.gateway.outbound_email.queue_outbound_email", sin_conexion)
    esc = await _renovacion_por_el_panel_de_un_negocio_del_alta(vendedores)
    await _entregar(esc.negocio, "platform.renewal_due", on_platform_renewal_due)

    renovacion = [c for c in correos if "vence" in c["subject"]]
    assert renovacion and all("/plan" in c["body"] for c in renovacion)
    [avisada] = await _auditoria(esc.negocio, "renovacion_avisada")
    assert (avisada.output["canal"], avisada.output["remitente"]) == ("panel", "plataforma")
