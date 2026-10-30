"""Conciliación diaria del cobro de la plataforma entre los dos canales (HU-SH-04, ADR-0123).

Compara, para cada negocio que creó el alta automática de un vendedor de la allowlist, los
dos lados del mismo cobro:

- **asistente:** la suscripción del cliente en el vendedor y sus cargos por período (por el
  contrato de suscripciones, `get`: el core no lee tablas de un pack);
- **negocio:** `tenant_subscriptions` y los pagos por período de `subscription_payments`.

Cada diferencia se alerta con su detalle: `audit_log` del vendedor, un push a sus dueños y la
alerta de operación (`send_alert`, Slack si está configurado). No corrige nada: cada caso tiene
su arreglo en `docs/runbooks/cobro-de-plataforma.md`.

Diferencias que detecta:

| Tipo | Qué significa |
|---|---|
| `pagado_en_el_asistente_sin_aplicar` | el cargo del período está pagado en el vendedor y el negocio no tiene ese período pagado (el evento no llegó o falló) |
| `pagado_en_el_panel_sin_sincronizar` | el negocio pagó el período por el panel y el cargo sigue pendiente en el vendedor (el asistente lo seguiría cobrando) |
| `pago_duplicado` | un período se pagó por los dos canales (de los últimos 7 días) |
| `plan_distinto` | el plan de la suscripción en el vendedor no es el del negocio |
| `monto_distinto` | un cargo pendiente del vendedor no cobra el precio del plan del negocio |
| `sin_suscripcion_en_el_asistente` | la suscripción del vendedor ya no está, y el negocio sigue con un plan pago vigente |
| `anticipacion_distinta` | el vendedor genera el cargo con otros `dias_anticipacion` que `BILLING_DIAS_RENOVACION` |
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.billing.canales import ACTOR, capacidad_del_vendedor, plan_del_cargo
from core.billing.periodos import etiqueta_de_referencia, tiene_periodo_pago
from core.billing.plans import get_plan
from core.config import get_settings
from core.db.models import ProvisioningRequest, SubscriptionPayment, TenantSubscription
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_ETIQUETA = re.compile(r"^\d{4}-\d{2}$")
_DUPLICADOS_RECIENTES = timedelta(days=7)
_MAX_EN_LA_ALERTA = 50


@dataclass(frozen=True)
class Diferencia:
    tipo: str
    negocio: str
    referencia: str | None = None
    detalle: dict[str, Any] = field(default_factory=dict)


def _referencia(slug: str, etiqueta: str) -> str:
    return f"plat-{slug}-{etiqueta.replace('-', '')}"


async def conciliar(now: datetime | None = None) -> dict[str, int]:
    """Concilia todos los vendedores de la allowlist. Devuelve el resumen de la corrida."""
    from core.provisioning.vendedores import es_vendedor, vendedores_configurados

    now = now or datetime.now(UTC)
    resumen = {"vendedores": 0, "negocios": 0, "diferencias": 0}
    for vendedor in sorted(vendedores_configurados()):
        if not await es_vendedor(vendedor):
            continue
        resumen["vendedores"] += 1
        negocios, diferencias = await conciliar_vendedor(vendedor, now=now)
        resumen["negocios"] += negocios
        resumen["diferencias"] += len(diferencias)
    log.info("billing_conciliacion", **resumen)
    return resumen


async def conciliar_vendedor(vendedor: uuid.UUID, *, now: datetime) -> tuple[int, list[Diferencia]]:
    """Concilia los negocios del vendedor y alerta las diferencias. (negocios, diferencias)."""
    from core.subscriptions import SubscriptionError

    async with tenant_scope(vendedor):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(ProvisioningRequest)
                    .where(
                        ProvisioningRequest.tenant_id == vendedor,
                        ProvisioningRequest.estado == "completado",
                        ProvisioningRequest.negocio_tenant_id.isnot(None),
                        ProvisioningRequest.subscription_id.isnot(None),
                        ProvisioningRequest.deleted_at.is_(None),
                    )
                    .order_by(ProvisioningRequest.created_at)
                )
            ).scalars().all()
        try:
            cap = await capacidad_del_vendedor(vendedor)
        except SubscriptionError:
            cap = None
        anticipacion = await _dias_de_anticipacion(cap)
    diferencias: list[Diferencia] = []
    renovacion = get_settings().billing_dias_renovacion
    if filas and anticipacion is not None and anticipacion != renovacion:
        # El asistente cobraría el período siguiente otro día que el panel (ADR-0123 § 7).
        diferencias.append(
            Diferencia(
                "anticipacion_distinta",
                "",
                None,
                {"dias_anticipacion_del_vendedor": anticipacion, "BILLING_DIAS_RENOVACION": renovacion},
            )
        )
    for fila in filas:
        try:
            diferencias.extend(await conciliar_negocio(vendedor, fila, cap, now=now))
        except Exception as exc:
            log.exception("billing_conciliacion_negocio_fallo", tenant_id=str(vendedor), negocio=fila.negocio_slug)
            diferencias.append(
                Diferencia("no_se_pudo_conciliar", fila.negocio_slug or "", None, {"error": type(exc).__name__})
            )
    if diferencias:
        await _alertar(vendedor, diferencias)
    return len(filas), diferencias


async def _dias_de_anticipacion(cap: Any) -> int | None:
    """`dias_anticipacion` de la config del proveedor de suscripciones del vendedor (bajo su
    scope), si la tiene."""
    from core.plugin_manager import plugin_manager

    if cap is None:
        return None
    try:
        config = await plugin_manager.load_config(cap.provider_id)
    except Exception:
        return None
    valor = getattr(config, "dias_anticipacion", None)
    return int(valor) if isinstance(valor, int) else None


async def conciliar_negocio(
    vendedor: uuid.UUID, fila: ProvisioningRequest, cap: Any, *, now: datetime
) -> list[Diferencia]:
    """Las diferencias de UN negocio. Cada lado se lee con su propio `tenant_scope`."""
    from core.subscriptions import SubscriptionError

    negocio = fila.negocio_tenant_id
    slug = fila.negocio_slug or ""
    if negocio is None or fila.subscription_id is None:
        return []

    vista: dict[str, Any] | None = None
    motivo_sin_vista: str | None = "sin_proveedor" if cap is None else None
    plan_asistente: str | None = None
    if cap is not None:
        async with tenant_scope(vendedor):
            try:
                vista = dict(await cap.get(fila.subscription_id))
            except SubscriptionError as exc:
                motivo_sin_vista = exc.code
            if vista is not None:
                plan_asistente = await plan_del_cargo(vendedor, vista.get("plan_id"))

    async with tenant_scope(negocio):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == negocio, TenantSubscription.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            pagos = (
                await session.execute(
                    select(SubscriptionPayment)
                    .where(
                        SubscriptionPayment.tenant_id == negocio,
                        SubscriptionPayment.periodo.isnot(None),
                        SubscriptionPayment.status.in_(("aprobado", "duplicado")),
                        SubscriptionPayment.deleted_at.is_(None),
                    )
                    .order_by(SubscriptionPayment.period_start.desc())
                    .limit(24)
                )
            ).scalars().all()

    aprobados = {
        etiqueta: pago
        for pago in pagos
        if pago.status == "aprobado" and (etiqueta := etiqueta_de_referencia(pago.periodo or ""))
    }
    cargos = {
        str(c.get("periodo")): c
        for c in (vista or {}).get("cargos") or []
        if _ETIQUETA.match(str(c.get("periodo") or ""))
    }
    diferencias: list[Diferencia] = []
    for etiqueta, cargo in sorted(cargos.items()):
        if cargo.get("estado") == "pagado" and etiqueta not in aprobados:
            diferencias.append(
                Diferencia(
                    "pagado_en_el_asistente_sin_aplicar",
                    slug,
                    _referencia(slug, etiqueta),
                    {"monto": cargo.get("monto")},
                )
            )
        elif cargo.get("estado") == "pendiente" and etiqueta in aprobados:
            diferencias.append(
                Diferencia(
                    "pagado_en_el_panel_sin_sincronizar",
                    slug,
                    aprobados[etiqueta].periodo,
                    {"pagado_por": aprobados[etiqueta].provider},
                )
            )
    for pago in pagos:
        if pago.status == "duplicado" and pago.paid_at is not None and pago.paid_at > now - _DUPLICADOS_RECIENTES:
            diferencias.append(
                Diferencia(
                    "pago_duplicado",
                    slug,
                    pago.periodo,
                    {"provider": pago.provider, "monto": str(pago.amount_cop)},
                )
            )
    if sub is not None and vista is not None and vista.get("estado") in ("activa", "trialing"):
        if plan_asistente is not None and plan_asistente != sub.plan_id:
            diferencias.append(
                Diferencia("plan_distinto", slug, None, {"asistente": plan_asistente, "negocio": sub.plan_id})
            )
        plan = get_plan(sub.plan_id)
        if plan is not None and not plan.is_free and plan_asistente == sub.plan_id:
            for etiqueta, cargo in sorted(cargos.items()):
                if cargo.get("estado") == "pendiente" and abs(float(cargo.get("monto") or 0) - plan.price_cop) > 0.01:
                    diferencias.append(
                        Diferencia(
                            "monto_distinto",
                            slug,
                            _referencia(slug, etiqueta),
                            {"asistente": cargo.get("monto"), "plan": plan.price_cop},
                        )
                    )
    if (
        vista is None
        and sub is not None
        and tiene_periodo_pago(sub.plan_id, sub.status)
        and sub.period_end is not None
        and sub.period_end > now
    ):
        diferencias.append(
            Diferencia(
                "sin_suscripcion_en_el_asistente",
                slug,
                None,
                {"motivo": motivo_sin_vista, "plan": sub.plan_id, "hasta": sub.period_end.isoformat()},
            )
        )
    return diferencias


async def _alertar(vendedor: uuid.UUID, diferencias: list[Diferencia]) -> None:
    """Alerta con el detalle: auditoría del vendedor, log por diferencia, push y `send_alert`."""
    from core.shared.alerts import send_alert

    detalle = [asdict(d) for d in diferencias[:_MAX_EN_LA_ALERTA]]
    async with tenant_scope(vendedor):
        await record_audit(
            actor=ACTOR,
            action="conciliacion_cobro_plataforma",
            output_data={"total": len(diferencias), "diferencias": detalle},
        )
    for diferencia in diferencias:
        log.warning(
            "billing_conciliacion_diferencia",
            tenant_id=str(vendedor),
            tipo=diferencia.tipo,
            negocio=diferencia.negocio,
            referencia=diferencia.referencia,
            detalle=diferencia.detalle,
        )
    lineas = [
        f"- {d.tipo}: {d.negocio} {d.referencia or ''} {json.dumps(d.detalle, ensure_ascii=False, default=str)}"
        for d in diferencias[:20]
    ]
    await send_alert(
        f"Conciliación del cobro de la plataforma: {len(diferencias)} diferencia(s)",
        "\n".join(lineas),
        severity="warning",
    )
    try:
        from core.tasks.push_tasks import notify_owners_push

        notify_owners_push.delay(
            str(vendedor),
            "Conciliación del cobro de planes",
            f"Hay {len(diferencias)} diferencia(s) entre el asistente y el panel. Revisa el detalle en la auditoría.",
            {"kind": "conciliacion_cobro_plataforma", "total": str(len(diferencias))},
        )
    except Exception:
        log.warning("billing_conciliacion_push_fallido", tenant_id=str(vendedor), exc_info=True)


__all__ = ["Diferencia", "conciliar", "conciliar_negocio", "conciliar_vendedor"]
