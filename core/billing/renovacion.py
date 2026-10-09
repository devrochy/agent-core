"""Renovación del plan de la plataforma (HU-SH-04, ADR-0123).

Antes no había un job de renovación: `core/tasks/billing_lifecycle.py` solo marcaba
`past_due`/`expired`, y el plan pago de un negocio vencía sin que nadie le cobrara el período
siguiente.

Cada día, para cada negocio con un plan pago cuyo período termina dentro de
`BILLING_DIAS_RENOVACION` días (5 por defecto), o que ya terminó hace menos de un período:

1. Genera el **cobro del período siguiente** con su referencia (`plat-<slug>-<AAAAMM>`): una
   fila `pendiente` en `subscription_payments` (`provider = 'plataforma'`, sin link todavía).
   Idempotente por la referencia: un período tiene un solo cobro de renovación.
2. En la misma transacción escribe `platform.renewal_due` en el outbox (ADR-0107). Su handler
   avisa por el **canal preferido** del negocio:
   - **asistente** (negocio del alta automática que paga por WhatsApp): el cargo con su link y
     su aviso por WhatsApp (plantilla fuera de 24 h, ADR-0108) los manda el cron
     `generar_cargos` del vendedor, con `dias_anticipacion` = `BILLING_DIAS_RENOVACION`. Aquí
     solo se confirma por el contrato (`get`) que esa suscripción puede cobrar;
   - **panel** (o si el asistente no puede cobrar): correo a los dueños con el enlace a
     `/plan` y un push. El cron del vendedor no le manda link a quien pagó el último período
     por el panel (`pago_externo`).

Un período ya pagado (por cualquiera de los dos canales) no genera cobro ni aviso.
"""

from __future__ import annotations

import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.billing.checkout import PROVEEDOR_RENOVACION
from core.billing.periodos import periodo_de, tiene_periodo_pago
from core.billing.plans import get_plan
from core.config import get_settings
from core.db.models import Admin, SubscriptionPayment, Tenant, TenantSubscription, TenantUser
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

_MAX_CORREOS = 5


def _fecha(valor: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(valor)) if valor else None
    except ValueError:
        return None


def _monto(valor: Any) -> str:
    from core.events.core_handlers import format_amount

    return format_amount(valor, "COP")


# ------------------------------------------------------------------ job
async def renovar_suscripciones(now: datetime | None = None) -> dict[str, int]:
    """Genera los cobros de renovación del día. Un negocio que falla no frena a los demás."""
    now = now or datetime.now(UTC)
    dias = get_settings().billing_dias_renovacion
    async with get_session() as session:
        tenant_ids = (
            await session.execute(
                select(Tenant.id).where(Tenant.deleted_at.is_(None), Tenant.status == "active")
            )
        ).scalars().all()
    totales = {"negocios": len(tenant_ids), "cobros": 0, "fallidos": 0}
    for tenant_id in tenant_ids:
        try:
            if await renovar_negocio(tenant_id, now=now, dias=dias) is not None:
                totales["cobros"] += 1
        except Exception:
            totales["fallidos"] += 1
            log.exception("billing_renovacion_fallo", tenant_id=str(tenant_id))
    if totales["cobros"] or totales["fallidos"]:
        log.info("billing_renovacion", **totales)
    return totales


async def renovar_negocio(tenant_id: uuid.UUID, *, now: datetime, dias: int) -> str | None:
    """El cobro del período siguiente del negocio, si le toca. Devuelve su referencia."""
    from core.events.outbox import emit_domain
    from core.shared.events import ON_PLATFORM_RENEWAL_DUE

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription)
                    .where(TenantSubscription.tenant_id == tenant_id, TenantSubscription.deleted_at.is_(None))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if sub is None or sub.period_end is None or not tiene_periodo_pago(sub.plan_id, sub.status):
                return None
            if sub.period_end - now > timedelta(days=dias):
                return None
            plan = get_plan(sub.plan_id)
            tenant = await session.get(Tenant, tenant_id)
            if plan is None or tenant is None:
                return None
            periodo = periodo_de(tenant.slug, sub.period_end, sub.dia_ancla or sub.period_end.day)
            if periodo.fin <= now:
                return None  # vencido hace más de un período: se paga uno nuevo desde /plan
            ya_hay = (
                await session.execute(
                    select(SubscriptionPayment.id)
                    .where(
                        SubscriptionPayment.tenant_id == tenant_id,
                        SubscriptionPayment.periodo == periodo.referencia,
                        SubscriptionPayment.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).first()
            if ya_hay is not None:
                return None  # ya hay un cobro (o un pago) de ese período
            session.add(
                SubscriptionPayment(
                    tenant_id=tenant_id,
                    subscription_id=sub.id,
                    plan_id=plan.id,
                    amount_cop=plan.price_cop,
                    currency=plan.currency,
                    status="pendiente",
                    provider=PROVEEDOR_RENOVACION,
                    provider_reference=periodo.referencia,
                    periodo=periodo.referencia,
                    period_start=periodo.inicio,
                    period_end=periodo.fin,
                )
            )
            await emit_domain(
                session,
                tenant_id=tenant_id,
                topic=ON_PLATFORM_RENEWAL_DUE,
                dedupe_key=f"plat_renewal:{periodo.referencia}",
                payload={
                    "referencia": periodo.referencia,
                    "plan_id": plan.id,
                    "plan_nombre": plan.name,
                    "monto": str(plan.price_cop),
                    "moneda": plan.currency,
                    "periodo_inicio": periodo.inicio.isoformat(),
                    "periodo_fin": periodo.fin.isoformat(),
                },
            )
    log.info("billing_renovacion_cobro", tenant_id=str(tenant_id), referencia=periodo.referencia)
    return periodo.referencia


# ------------------------------------------------------------------ aviso
async def _periodo_pagado(negocio: uuid.UUID, referencia: str) -> bool:
    async with get_session() as session:
        return (
            await session.execute(
                select(SubscriptionPayment.id)
                .where(
                    SubscriptionPayment.tenant_id == negocio,
                    SubscriptionPayment.periodo == referencia,
                    SubscriptionPayment.status == "aprobado",
                    SubscriptionPayment.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).first() is not None


async def on_platform_renewal_due(**payload: Any) -> None:
    """Handler de `platform.renewal_due` (outbox, bajo el `tenant_scope` del negocio): avisa
    del cobro del período siguiente por el canal preferido. Si el período ya se pagó, nada."""
    from core.billing.canales import ACTOR, CANAL_ASISTENTE, CANAL_PANEL, canal_preferido

    negocio = get_current_tenant_id()
    referencia = str(payload.get("referencia") or "")
    inicio = _fecha(payload.get("periodo_inicio"))
    if negocio is None or not referencia or inicio is None:
        log.warning("billing_renovacion_evento_invalido")
        return
    if await _periodo_pagado(negocio, referencia):
        return
    canal, vinculo = await canal_preferido(negocio)
    detalle: dict[str, Any] = {}
    if canal == CANAL_ASISTENTE and vinculo is not None:
        # El cargo, su link y su aviso por WhatsApp los manda el cron `generar_cargos` del
        # proveedor del vendedor (`dias_anticipacion` = `BILLING_DIAS_RENOVACION`). Aquí solo se
        # confirma, por el contrato (`get`), que esa suscripción puede cobrar.
        estado = await _estado_en_el_vendedor(vinculo)
        detalle = {"suscripcion_en_el_asistente": estado}
        if estado not in ("activa", "trialing"):
            # Cancelada, vencida, pausada o ilegible: el aviso sale por el panel y la
            # conciliación lo muestra.
            log.warning("billing_renovacion_asistente_no_cobra", tenant_id=str(negocio), estado=estado)
            canal = CANAL_PANEL
    if canal == CANAL_PANEL:
        detalle = {**detalle, **await _avisar_por_el_panel(negocio, payload)}
    await record_audit(
        actor=ACTOR,
        action="renovacion_avisada",
        input_data={"referencia": referencia},
        output_data={"canal": canal, **{k: v for k, v in detalle.items() if k != "link"}},
    )
    log.info("billing_renovacion_avisada", tenant_id=str(negocio), referencia=referencia, canal=canal)


async def _estado_en_el_vendedor(vinculo: Any) -> str | None:
    """Estado de la suscripción del negocio en el vendedor (operación `get` del contrato)."""
    from core.billing.canales import capacidad_del_vendedor
    from core.subscriptions import SubscriptionError

    try:
        async with tenant_scope(vinculo.vendedor):
            cap = await capacidad_del_vendedor(vinculo.vendedor)
            if cap is None:
                return None
            vista = await cap.get(vinculo.subscription_id)
    except SubscriptionError as exc:
        return exc.code
    return str(vista.get("estado") or "")


async def _correos_de_los_duenos(negocio: uuid.UUID) -> list[str]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(Admin.email)
                    .join(TenantUser, TenantUser.admin_id == Admin.id)
                    .where(TenantUser.tenant_id == negocio, TenantUser.role == "owner", Admin.active.is_(True))
                    .order_by(Admin.email)
                    .limit(_MAX_CORREOS)
                )
            ).scalars().all()
        )


def asunto_y_cuerpo(payload: dict[str, Any], enlace: str) -> tuple[str, str]:
    """Correo de la renovación por el panel. Solo datos del catálogo y del período: nada que
    haya escrito un cliente ni un modelo."""
    plan = str(payload.get("plan_nombre") or payload.get("plan_id") or "tu plan")
    inicio = _fecha(payload.get("periodo_inicio"))
    vence = inicio.strftime("%d/%m/%Y") if inicio else ""
    asunto = f"Tu plan {plan} de SolversHub vence el {vence}"
    cuerpo = (
        "Hola,\n\n"
        f"El período de tu plan {plan} en SolversHub vence el {vence}. El cobro del período "
        f"siguiente ({payload.get('referencia')}) es de {_monto(payload.get('monto'))}.\n\n"
        f"Puedes pagarlo desde el panel, en Plan: {enlace}\n\n"
        "Si ya lo pagaste, ignora este correo."
    )
    return asunto, cuerpo


async def _avisar_por_el_panel(negocio: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
    """Correo a los dueños con el enlace a `/plan` y un push. Un fallo del SMTP se propaga:
    el outbox reintenta el evento."""
    from core.admin.invitations_service import url_del_panel
    from core.gateway.system_email import send_system_email

    asunto, cuerpo = asunto_y_cuerpo(payload, f"{url_del_panel()}/plan")
    enviados = 0
    sin_smtp = False
    for email in await _correos_de_los_duenos(negocio):
        try:
            if await send_system_email(email, asunto, cuerpo) is None:
                sin_smtp = True
            else:
                enviados += 1
        except (OSError, smtplib.SMTPException):
            log.warning("billing_renovacion_correo_fallido", tenant_id=str(negocio), exc_info=True)
            raise
    try:
        from core.tasks.push_tasks import notify_owners_push

        notify_owners_push.delay(
            str(negocio),
            "Tu plan vence pronto",
            f"El cobro del período {payload.get('referencia')} ya está listo. Págalo desde Plan.",
            {"kind": "renovacion_plataforma", "referencia": str(payload.get("referencia"))},
        )
    except Exception:
        log.warning("billing_renovacion_push_fallido", tenant_id=str(negocio), exc_info=True)
    return {"correos": enviados, "sin_smtp": sin_smtp}


__all__ = ["asunto_y_cuerpo", "on_platform_renewal_due", "renovar_negocio", "renovar_suscripciones"]
