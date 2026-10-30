"""Los dos canales del cobro de la plataforma, con una sola verdad (HU-SH-04, ADR-0123).

- **Canal asistente:** la suscripción del cliente dentro del negocio VENDEDOR (el de
  `solvershub`, en la allowlist `PROVISIONING_SELLER_TENANTS`). El cargo de cada período y su
  link los genera el proveedor de suscripciones del vendedor (`pack_subscriptions`).
- **Canal panel:** `/plan` del negocio, con el link de la plataforma (`core/billing/checkout`).
- **Fuente única:** `tenant_subscriptions` del negocio (lo que lee el enforcement). Los dos
  canales aplican el pago con `aplicar_pago_de_periodo` y la misma referencia de período
  (`plat-<slug>-<AAAAMM>`): un período se aplica una sola vez.

Este módulo une los dos lados:

- `vinculo_asistente`: del negocio a su suscripción en el vendedor. El vínculo es la fila del
  alta (`provisioning_requests`, del vendedor): el negocio no guarda ids internos del vendedor.
- `on_subscription_charge_paid` (outbox, bajo el `tenant_scope` del vendedor): el cargo de un
  período pagado por el asistente → período aplicado al negocio.
- `on_platform_period_paid` (outbox, bajo el `tenant_scope` del negocio): un período pagado por
  el panel → el cargo del mismo período queda pagado en el vendedor (sincronización inversa) y
  el asistente no lo vuelve a cobrar.

El core entra al `tenant_scope` de cada lado por separado: nunca lee filas de un negocio con
el scope de otro. Solo cruza la frontera para negocios aprovisionados por un vendedor de la
allowlist (fail-closed).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from core.billing.periodos import Periodo, etiqueta_de_referencia, referencia_del_periodo
from core.db.models import ProvisioningRequest, Tenant
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

CANAL_ASISTENTE = "asistente"
CANAL_PANEL = "panel"
# Actor de la auditoría del cobro de la plataforma (no es una persona ni un token).
ACTOR = "billing"


@dataclass(frozen=True)
class VinculoAsistente:
    """El negocio y su suscripción en el negocio vendedor (canal asistente)."""

    vendedor: uuid.UUID
    subscription_id: uuid.UUID
    registro_id: uuid.UUID
    customer_id: uuid.UUID | None


def _fecha(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        return valor
    try:
        return datetime.fromisoformat(str(valor))
    except (TypeError, ValueError):
        return None


def _uuid(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ vínculo
async def vinculo_asistente(negocio: uuid.UUID) -> VinculoAsistente | None:
    """Suscripción del negocio en su vendedor, o None si el negocio no tiene canal asistente
    (no lo creó el alta automática, el alta no terminó o el vendedor salió de la allowlist)."""
    from core.provisioning.vendedores import es_vendedor, vendedores_configurados

    for vendedor in sorted(vendedores_configurados()):
        if vendedor == negocio or not await es_vendedor(vendedor):
            continue
        async with tenant_scope(vendedor):
            async with get_session() as session:
                fila = (
                    await session.execute(
                        select(ProvisioningRequest)
                        .where(
                            ProvisioningRequest.tenant_id == vendedor,
                            ProvisioningRequest.negocio_tenant_id == negocio,
                            ProvisioningRequest.estado == "completado",
                            ProvisioningRequest.subscription_id.isnot(None),
                            ProvisioningRequest.deleted_at.is_(None),
                        )
                        .order_by(ProvisioningRequest.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        if fila is not None and fila.subscription_id is not None:
            return VinculoAsistente(vendedor, fila.subscription_id, fila.id, fila.customer_id)
    return None


async def registro_de_la_suscripcion(vendedor: uuid.UUID, subscription_id: uuid.UUID) -> ProvisioningRequest | None:
    """La fila del alta de una suscripción del vendedor (bajo su `tenant_scope`)."""
    async with get_session() as session:
        return (
            await session.execute(
                select(ProvisioningRequest).where(
                    ProvisioningRequest.tenant_id == vendedor,
                    ProvisioningRequest.subscription_id == subscription_id,
                    ProvisioningRequest.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def slug_de(tenant_id: uuid.UUID) -> str | None:
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    return tenant.slug if tenant is not None else None


async def capacidad_del_vendedor(vendedor: uuid.UUID) -> Any:
    """Proveedor de suscripciones del vendedor (bajo su `tenant_scope`)."""
    from core.subscriptions import capability_del_negocio

    return await capability_del_negocio(vendedor)


async def plan_del_cargo(vendedor: uuid.UUID, plan_id: Any) -> str | None:
    """Plan de la plataforma del plan del catálogo de la suscripción (`plan_id`, SPEC-43), bajo el
    scope del vendedor. None si no vende uno."""
    from core.provisioning.fuentes import fuentes_de_alta, plan_plataforma_de

    # Un fallo al leer el catálogo se propaga: el outbox reintenta (no es «sin plan»).
    fuentes = await fuentes_de_alta.del_negocio(vendedor)
    return await plan_plataforma_de(fuentes, plan_id=str(plan_id) if plan_id else None)


async def canal_preferido(negocio: uuid.UUID) -> tuple[str, VinculoAsistente | None]:
    """Por dónde se le avisa y se le cobra al negocio el período siguiente.

    El canal por el que pagó la última vez: un negocio sin canal asistente (no lo creó el
    alta automática) usa el panel; uno aprovisionado usa el asistente hasta que pague por
    el panel (decisión de ADR-0123 a falta de una preferencia explícita)."""
    from core.billing.service import get_active_subscription

    vinculo = await vinculo_asistente(negocio)
    if vinculo is None:
        return CANAL_PANEL, None
    sub = await get_active_subscription(negocio)
    if sub is not None and sub.provider not in (None, CANAL_ASISTENTE, "plataforma"):
        return CANAL_PANEL, vinculo
    return CANAL_ASISTENTE, vinculo


async def estado_en_el_asistente(vinculo: VinculoAsistente, etiqueta: str) -> str | None:
    """Estado del cargo del período `etiqueta` (`AAAA-MM`) en el vendedor, o None si no hay
    cargo o no se pudo leer. Lo usa el panel para no cobrar un período que el asistente ya
    cobró y todavía no llegó al negocio (el evento está en camino)."""
    try:
        async with tenant_scope(vinculo.vendedor):
            cap = await capacidad_del_vendedor(vinculo.vendedor)
            if cap is None:
                return None
            vista = await cap.get(vinculo.subscription_id)
    except Exception:
        log.warning("cobro_plataforma_estado_asistente_fallo", tenant_id=str(vinculo.vendedor), exc_info=True)
        return None
    for cargo in vista.get("cargos") or []:
        if cargo.get("periodo") == etiqueta and cargo.get("estado") == "pagado":
            return "pagado"
    for cargo in vista.get("cargos") or []:
        if cargo.get("periodo") == etiqueta:
            return str(cargo.get("estado"))
    return None


# ------------------------------------------------------------------ alertas
def por_debajo_del_precio(plan_id: str, monto: Any) -> bool:
    """¿Lo pagado es menos que el precio del plan de la plataforma? Un monto ilegible cuenta
    como por debajo (no se aplica un plan sin saber cuánto se pagó)."""
    from decimal import Decimal, InvalidOperation

    from core.billing.plans import get_plan

    plan = get_plan(plan_id)
    if plan is None:
        return True
    try:
        pagado = Decimal(str(monto))
    except (InvalidOperation, ValueError):
        return True
    return not pagado.is_finite() or pagado < Decimal(plan.price_cop)


async def registrar_monto_distinto(
    negocio: uuid.UUID,
    periodo: Periodo,
    *,
    plan_id: str,
    monto: Any,
    origen: str,
    vendedor: uuid.UUID | None,
) -> None:
    """El asistente cobró un período o un cambio de plan por menos que el precio del plan: no
    se aplica. Queda un pago `monto_distinto` en el negocio, auditado en los dos lados, y se
    alerta para revisarlo a mano."""
    from decimal import Decimal, InvalidOperation

    from core.billing.plans import get_plan
    from core.billing.service import _suscripcion_bloqueada
    from core.db.models import SubscriptionPayment
    from core.shared.alerts import send_alert

    plan = get_plan(plan_id)
    try:
        importe = Decimal(str(monto))
    except (InvalidOperation, ValueError):
        importe = Decimal(0)
    referencia_pago = f"{periodo.referencia}:monto:{uuid.uuid5(uuid.NAMESPACE_URL, origen).hex[:12]}"
    async with tenant_scope(negocio):
        async with get_session() as session:
            sub = await _suscripcion_bloqueada(session, negocio, datetime.now(UTC))
            existe = (
                await session.execute(
                    select(SubscriptionPayment.id).where(
                        SubscriptionPayment.tenant_id == negocio,
                        SubscriptionPayment.provider_reference == referencia_pago,
                        SubscriptionPayment.deleted_at.is_(None),
                    )
                )
            ).first()
            if existe is not None:
                return  # ya registrado y alertado (reintento del evento)
            session.add(
                SubscriptionPayment(
                    tenant_id=negocio,
                    subscription_id=sub.id,
                    plan_id=plan_id,
                    amount_cop=importe if importe.is_finite() else Decimal(0),
                    currency=plan.currency if plan else "COP",
                    status="monto_distinto",
                    provider=CANAL_ASISTENTE,
                    provider_reference=referencia_pago,
                    periodo=periodo.referencia,
                    period_start=periodo.inicio,
                    period_end=periodo.fin,
                    paid_at=datetime.now(UTC),
                )
            )
        detalle = {"referencia": periodo.referencia, "plan": plan_id, "monto": str(monto), "origen": origen}
        await record_audit(actor=ACTOR, action="cobro_plataforma_monto_distinto", output_data=detalle)
    slug = await slug_de(negocio)
    if vendedor is not None:
        async with tenant_scope(vendedor):
            await record_audit(
                actor=ACTOR, action="cobro_plataforma_monto_distinto", output_data={**detalle, "negocio": slug}
            )
    log.error("cobro_plataforma_monto_distinto", tenant_id=str(negocio), **detalle)
    await send_alert(
        "Plan cobrado por menos que su precio",
        f"{slug}: {origen} por {monto} para el plan {plan_id} ({periodo.referencia}). No se aplicó: revisarlo.",
        severity="critical",
    )


async def alertar_pago_duplicado(
    negocio: uuid.UUID,
    referencia: str,
    *,
    canal: str,
    cubierto_por: str | None,
    vendedor: uuid.UUID | None,
) -> None:
    """Un período se pagó dos veces (por los dos canales): la plata llegó dos veces y la
    suscripción se extendió una sola. Lo resuelve una persona (devolver o abonar al período
    siguiente, `docs/runbooks/cobro-de-plataforma.md`)."""
    from core.shared.alerts import send_alert

    slug = await slug_de(negocio)
    detalle = {"referencia": referencia, "canal": canal, "cubierto_por": cubierto_por}
    async with tenant_scope(negocio):
        await record_audit(actor=ACTOR, action="cobro_plataforma_pago_duplicado", output_data=detalle)
    if vendedor is not None:
        async with tenant_scope(vendedor):
            await record_audit(
                actor=ACTOR,
                action="cobro_plataforma_pago_duplicado",
                output_data={**detalle, "negocio": slug},
            )
        try:
            from core.tasks.push_tasks import notify_owners_push

            notify_owners_push.delay(
                str(vendedor),
                "Pago doble de un plan",
                f"{slug}: el período {referencia} se pagó por el {canal} y ya estaba pagado. Hay que devolverlo o abonarlo.",
                {"kind": "cobro_plataforma_pago_duplicado", "referencia": referencia},
            )
        except Exception:
            log.warning("cobro_plataforma_push_fallido", referencia=referencia, exc_info=True)
    await send_alert(
        "Pago doble del plan de un negocio",
        f"{slug}: el período {referencia} se pagó por el {canal} y ya estaba pagado por {cubierto_por}. "
        "Devolver o abonar (runbook cobro-de-plataforma).",
        severity="critical",
    )


# ------------------------------------------------------------------ asistente → negocio
async def on_subscription_charge_paid(**payload: Any) -> None:
    """Handler de `subscription.charge_paid` (outbox, bajo el `tenant_scope` del vendedor).

    El cargo de un período de la suscripción de un negocio aprovisionado quedó pagado por el
    asistente: el período se aplica a `tenant_subscriptions` del negocio. Para el resto de los
    negocios (no son vendedores o la suscripción no es la de un negocio aprovisionado) no hace
    nada. Idempotente por la referencia del período."""
    from core.billing.service import DUPLICADO, aplicar_pago_de_periodo, get_active_subscription
    from core.provisioning.vendedores import es_vendedor

    vendedor = get_current_tenant_id()
    if vendedor is None or not await es_vendedor(vendedor):
        return
    subscription_id = _uuid(payload.get("subscription_id"))
    if subscription_id is None:
        log.warning("cobro_plataforma_evento_invalido", tenant_id=str(vendedor))
        return
    registro = await registro_de_la_suscripcion(vendedor, subscription_id)
    if registro is None:
        return  # un cliente del vendedor que no compró un negocio
    if registro.estado == "en_curso":
        # El alta todavía corre (no debería pasar: el primer cargo llega un período después):
        # el outbox reintenta.
        raise RuntimeError(f"el alta {registro.referencia} todavía está en curso")
    negocio = registro.negocio_tenant_id
    if registro.estado != "completado" or negocio is None:
        return
    inicio, fin = _fecha(payload.get("periodo_inicio")), _fecha(payload.get("periodo_fin"))
    slug = registro.negocio_slug or await slug_de(negocio)
    if inicio is None or fin is None or not slug:
        log.error("cobro_plataforma_cargo_sin_periodo", tenant_id=str(vendedor), charge_id=payload.get("charge_id"))
        return
    periodo = Periodo(inicio=inicio, fin=fin, referencia=referencia_del_periodo(slug, inicio))
    plan_id = await plan_del_cargo(vendedor, payload.get("plan_id"))
    if plan_id is None:
        actual = await get_active_subscription(negocio)
        plan_id = actual.plan_id if actual is not None else None
    if plan_id is None:
        log.error("cobro_plataforma_sin_plan", tenant_id=str(vendedor), referencia=periodo.referencia)
        return
    if por_debajo_del_precio(plan_id, payload.get("monto")):
        # Un cargo por menos que el plan (p. ej. una suscripción creada a mano con otro precio)
        # no paga ese plan: no se aplica y se alerta.
        await registrar_monto_distinto(
            negocio,
            periodo,
            plan_id=plan_id,
            monto=payload.get("monto"),
            origen=f"cargo {payload.get('charge_id')}",
            vendedor=vendedor,
        )
        return
    ancla = payload.get("dia_ancla")
    async with tenant_scope(negocio):
        async with get_session() as session:
            resultado = await aplicar_pago_de_periodo(
                session,
                negocio,
                plan_id=plan_id,
                periodo=periodo,
                provider=CANAL_ASISTENTE,
                provider_reference=periodo.referencia,
                monto=payload.get("monto"),
                plan_en_el_acto=False,
                dia_ancla=int(ancla) if isinstance(ancla, int | str) and str(ancla).isdigit() else None,
            )
        await record_audit(
            actor=ACTOR,
            action="cobro_plataforma_aplicado",
            input_data={"referencia": periodo.referencia, "canal": CANAL_ASISTENTE},
            output_data={"resultado": resultado.resultado, "plan": plan_id},
        )
    await record_audit(
        actor=ACTOR,
        action="cobro_plataforma_aplicado",
        input_data={
            "referencia": periodo.referencia,
            "canal": CANAL_ASISTENTE,
            "charge_id": payload.get("charge_id"),
            "subscription_id": str(subscription_id),
        },
        output_data={"resultado": resultado.resultado, "negocio": slug, "plan": plan_id},
    )
    log.info(
        "cobro_plataforma_asistente",
        tenant_id=str(vendedor),
        negocio=slug,
        referencia=periodo.referencia,
        resultado=resultado.resultado,
    )
    if resultado.resultado == DUPLICADO and resultado.nuevo:
        await alertar_pago_duplicado(
            negocio, periodo.referencia, canal=CANAL_ASISTENTE, cubierto_por=resultado.cubierto_por, vendedor=vendedor
        )


# ------------------------------------------------------------------ panel → asistente
async def on_platform_period_paid(**payload: Any) -> None:
    """Handler de `platform.period_paid` (outbox, bajo el `tenant_scope` del negocio).

    Sincronización inversa: el período que el negocio pagó por el panel queda pagado también
    en el canal asistente, así el asistente no lo vuelve a cobrar ni a recordar. Un negocio
    sin canal asistente no tiene nada que sincronizar. Si el vendedor no puede registrarlo, el
    evento falla y el outbox reintenta (al agotarse, queda en la Bandeja de fallos y la
    conciliación lo muestra)."""
    from core.db.models import SubscriptionPayment
    from core.subscriptions import registrar_pago_externo

    negocio = get_current_tenant_id()
    if negocio is None:
        return
    referencia = str(payload.get("referencia") or "")
    if not referencia:
        log.warning("cobro_plataforma_evento_invalido", tenant_id=str(negocio))
        return
    # El período sale del pago APROBADO del negocio, no del payload: un evento mal formado o
    # repetido no puede marcar pagado en el vendedor algo que el negocio no pagó.
    async with get_session() as session:
        pago = (
            await session.execute(
                select(SubscriptionPayment).where(
                    SubscriptionPayment.tenant_id == negocio,
                    SubscriptionPayment.periodo == referencia,
                    SubscriptionPayment.status == "aprobado",
                    SubscriptionPayment.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if pago is None or pago.period_start is None or pago.period_end is None:
        log.warning("cobro_plataforma_periodo_sin_pago", tenant_id=str(negocio), referencia=referencia)
        return
    inicio, fin = pago.period_start, pago.period_end
    vinculo = await vinculo_asistente(negocio)
    if vinculo is None:
        return
    async with tenant_scope(vinculo.vendedor):
        cap = await capacidad_del_vendedor(vinculo.vendedor)
        if cap is None:
            log.warning("cobro_plataforma_vendedor_sin_suscripciones", tenant_id=str(vinculo.vendedor))
            return
        resultado = await registrar_pago_externo(
            cap, vinculo.subscription_id, inicio=inicio, fin=fin, referencia=referencia, canal=CANAL_PANEL
        )
        estado = (resultado or {}).get("estado", "sin_soporte")
        await record_audit(
            actor=ACTOR,
            action="cobro_plataforma_sincronizado",
            input_data={"referencia": referencia, "canal": CANAL_PANEL, "subscription_id": str(vinculo.subscription_id)},
            output_data={"estado": estado, "negocio": await slug_de(negocio)},
        )
    log.info(
        "cobro_plataforma_sincronizacion_inversa",
        tenant_id=str(negocio),
        referencia=referencia,
        estado=estado,
        etiqueta=etiqueta_de_referencia(referencia),
    )


__all__ = [
    "ACTOR",
    "CANAL_ASISTENTE",
    "CANAL_PANEL",
    "VinculoAsistente",
    "alertar_pago_duplicado",
    "canal_preferido",
    "estado_en_el_asistente",
    "on_platform_period_paid",
    "on_subscription_charge_paid",
    "plan_del_cargo",
    "registro_de_la_suscripcion",
    "slug_de",
    "vinculo_asistente",
]
