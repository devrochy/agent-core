"""SubscriptionsModule: suscripciones de cliente final (ADR-0083; planes e inscripción: ADR-0177).

SPEC-43: el pack es dueño del catálogo de planes y de la inscripción (`planes.py`,
`inscripcion.py`): un plan de pago nace `pendiente_pago` y se activa con `payment.confirmed` de su
cargo (`sub-<cargo>`) o con la confirmación manual del panel. Camino viejo (hasta HU-COM-17):
cuando un pedido de commerce con un plan se paga, commerce entrega un snapshot y este pack
crea/activa la `Subscription` a través del registry del core (`activate`) — sin importar commerce
(invariante nº5). También escucha `payment.confirmed` para marcar los cargos pagados y expone la API
admin de planes e inscripción.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel
from sqlalchemy import select

import pack_subscriptions.jobs  # noqa: F401  # registra el @register_cron (ADR-0083/#156)
from core.db.session import get_session
from core.mcp.types import BaseModule, GuardrailResult
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from core.subscriptions import subscription_registry
from pack_subscriptions import (
    providers as _lead_provider,  # noqa: F401  # vista de Clientes (SPEC-35, ADR-0056)
)
from pack_subscriptions.admin_api import router as subscriptions_admin_api_router
from pack_subscriptions.capability import (
    SubscriptionCapabilityImpl,
    crear_suscripcion_para,
    reactivar_por_pago,
)
from pack_subscriptions.config import SubscriptionConfig
from pack_subscriptions.inscripcion import _activar_por_pago
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import sumar_periodo

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"

_PAUSAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?pausad\w*|paus\w+\s+(?:tu\s+)?suscripci", re.IGNORECASE)
_CANCELAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:cancel\w*|anulad\w*)|cancel\w+\s+(?:tu\s+)?suscripci", re.IGNORECASE)


# `crear_suscripcion_para` vive en capability.py (proveedor del contrato v1,
# ADR-0106); se re-exporta para la API admin y los tests que la importan de acá.
__all__ = ["crear_suscripcion_para"]


class SubscriptionsModule(BaseModule):
    id = PLUGIN_ID
    name = "Suscripciones"
    version = "0.3.0"
    description = "Suscripción de cliente final activada por pago de un plan (ADR-0083)"
    display_name = "Suscripciones"
    category = "Cobros"
    min_plan = "basic"
    required_connections: ClassVar[list[str]] = ["pack_payments"]
    action_summary: ClassVar[list[str]] = ["Cobrar el período", "Enviar link", "Gestionar estado"]
    setup_guide = "Define tus planes con periodicidad y activa los cobros recurrentes."
    requires_config = True
    config_schema = SubscriptionConfig
    declared_skills: list[type] = []  # noqa: RUF012 -- las tools sub_* viven en el core (ADR-0106)
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Suscripciones", "path": "/suscripciones", "section": "Operación", "section_key": "pack_subscriptions:suscripciones"},
    ]

    def on_activate(self, host: MCPHost) -> None:
        host.register_guardrail(
            "subs_completion_check",
            self._completion_guardrail,
            priority=6,
            etiqueta="Cambios de suscripción",
            explicacion="Evita que el asistente diga que cambió, pausó o canceló una suscripción si la acción no se hizo.",
        )
        subscription_registry.register(PLUGIN_ID, SubscriptionCapabilityImpl())
        # Datos de la inscripción para el alta automática de negocios (SPEC-43, HU-SUB-30).
        from core.provisioning.fuentes import fuentes_de_alta
        from pack_subscriptions.altas import DatosDeAltaDeLaInscripcion

        fuentes_de_alta.register(PLUGIN_ID, DatosDeAltaDeLaInscripcion())
        host.subscribe("payment.confirmed", self._on_payment_confirmed)
        # HU-AOS-08: procedimiento `cambiar_plan` y su resolver (solo los usa el motor, detrás de
        # `tenants.aos_procedures`).
        from pack_subscriptions.procedimientos import registrar

        registrar(host, PLUGIN_ID)

    def on_deactivate(self) -> None:
        subscription_registry.unregister(PLUGIN_ID)
        from core.provisioning.fuentes import fuentes_de_alta

        fuentes_de_alta.unregister(PLUGIN_ID)

    # `TENANTS_ROOT/<slug>/planes.json` (HU-SUB-32): el seed genérico lo busca además de `<pack>.json`.
    archivos_de_seed: tuple[str, ...] = ("planes.json",)

    async def seed_data(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
        """Catálogo de planes del negocio, idempotente y todo o nada (SPEC-43, HU-SUB-32)."""
        from pack_subscriptions.seed import seed_planes

        return await seed_planes(tenant_id, data)

    async def sembrar_escenario(self, tenant_id: uuid.UUID, escenario: str, entrada: Any) -> Any:
        """Suscripciones activa, por vencer y vencida con cargos y pagos (HU-QA-13)."""
        from core.qa.contrato_datos import EscenarioNoDisponible
        from pack_subscriptions.escenarios import sembrar

        if escenario != "suscripciones":
            raise EscenarioNoDisponible(f"Suscripciones no tiene el escenario {escenario}.")
        return await sembrar(tenant_id, entrada)

    def formas_de_datos_de_prueba(self) -> list[FormaDeDatos]:
        """Clientes y suscripciones de ejemplo (HU-QA-10, modo `suscripciones`)."""
        from pack_subscriptions.datos_de_prueba import formas

        return formas()

    async def sembrar_datos_de_prueba(
        self, tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
    ) -> list[RegistroSembrado]:
        from pack_subscriptions.datos_de_prueba import sembrar

        return await sembrar(tenant_id, modo, datos, contexto)

    async def retirar_datos_de_prueba(
        self, tenant_id: uuid.UUID, registros: list[RegistroSembrado]
    ) -> int:
        from pack_subscriptions.datos_de_prueba import retirar

        return await retirar(tenant_id, registros)

    def admin_api_routes(self) -> list[Any]:
        """API admin de suscripciones (ADR-0079), montada en `/api/v1`."""
        return [subscriptions_admin_api_router]

    async def _on_payment_confirmed(self, reference: str, provider: str, provider_reference: str, amount: float) -> None:
        """Marca pagado el cargo `sub-<charge.id>` (HU-SUB-04, defecto 6).

        - El monto tiene que coincidir con el del cargo: si no, no se marca pagado y se
          avisa al negocio (push a los dueños + `audit_log`).
        - Reactiva: una `vencida` (o una prueba) pagada vuelve a `activa`, y el período
          siguiente se calcula desde el período pagado.
        - Idempotente: un cargo ya pagado no se vuelve a tocar (el outbox reintenta).
        - HU-SH-04 (ADR-0123): emite `subscription.charge_paid` por el outbox, en la misma
          transacción, para que el core aplique el período al plan del negocio si la
          suscripción es la de un negocio aprovisionado. Si el cargo ya lo había pagado OTRO
          canal (`pago_externo`), la plata llegó dos veces: se avisa y el evento lo dice.
        """
        if not reference or not reference.startswith("sub-"):
            return
        try:
            charge_id = uuid.UUID(reference[4:])
        except (ValueError, TypeError):
            return
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                # Orden de locks del pack (design.md): suscripción → cargo. Se lee el cargo SIN lock para
                # saber su suscripción, se bloquea la suscripción y se vuelve a leer el cargo con
                # FOR UPDATE; así no forma deadlock con `change_plan`/`cancel`/la expiración, que bloquean
                # la suscripción y luego tocan sus cargos.
                previo = (
                    await session.execute(
                        select(SubscriptionCharge.subscription_id).where(
                            SubscriptionCharge.tenant_id == tenant_id, SubscriptionCharge.id == charge_id
                        )
                    )
                ).scalar_one_or_none()
                if previo is None:
                    return
                sub = (
                    await session.execute(
                        select(Subscription)
                        .where(Subscription.tenant_id == tenant_id, Subscription.id == previo)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                cargo = (
                    await session.execute(
                        select(SubscriptionCharge)
                        .where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            SubscriptionCharge.id == charge_id,
                        )
                        .with_for_update()  # dos entregas a la vez no aplican dos veces
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if cargo is None:
                    return
                if cargo.estado == "pagado":
                    if cargo.pago_externo and cargo.tipo == "periodo":
                        # Lo pagó otro canal y además se pagó el link del pack: pago doble.
                        if sub is not None:
                            await _emitir_cargo_pagado(session, cargo, sub, provider_reference, duplicado=True)
                        await _alertar_pago_sin_aplicar(
                            tenant_id,
                            cargo,
                            f"el período ya lo había pagado otro canal ({cargo.pago_externo.get('canal')}): pago doble",
                        )
                    return
                if cargo.estado != "pendiente":
                    # Pago de un cargo anulado (p. ej. un cambio de plan reemplazado o una inscripción
                    # expirada): no se aplica nada y se avisa al negocio (auditoría HU-SUB-07).
                    await _alertar_pago_sin_aplicar(tenant_id, cargo, f"el cargo estaba {cargo.estado}")
                    return
                if abs(Decimal(str(amount)) - Decimal(cargo.monto or 0)) > Decimal("0.01"):
                    log.error(
                        "subs_pago_monto_distinto",
                        reference=reference,
                        esperado=str(cargo.monto),
                        recibido=str(amount),
                    )
                    await _alertar_monto_distinto(tenant_id, cargo, amount, provider_reference)
                    return
                if sub is not None and cargo.tipo in ("inscripcion", "cambio_plan"):
                    # Activa por el único camino (el mismo de la confirmación manual del panel).
                    motivo = await _activar_por_pago(session, sub, cargo)
                    if motivo:
                        await _alertar_pago_sin_aplicar(tenant_id, cargo, motivo)
                    return
                cargo.estado = "pagado"
                if sub is not None:
                    reactivar_por_pago(sub, cargo)
                    if cargo.tipo == "periodo":
                        await _emitir_cargo_pagado(session, cargo, sub, provider_reference, duplicado=False)
        log.info("subs_charge_pagado", reference=reference)

    async def _completion_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Bloquea afirmar cambios de suscripción sin la tool correspondiente (ADR-0083)."""
        called = {r.get("_tool_name") for r in (tool_results or [])}
        checks = [
            (_PAUSAR, {"sub_pausar"}),
            (_CANCELAR, {"sub_cancelar"}),
        ]
        for pattern, tools in checks:
            if pattern.search(response_text or "") and not (called & tools):
                return GuardrailResult(
                    passed=False,
                    reason="Afirmación de suscripción sin la tool correspondiente en el turno.",
                    retry_instruction=(
                        f"Afirmaste algo sobre la suscripción pero no llamaste la herramienta "
                        f"({', '.join(sorted(tools))}). Llamala ahora, o si no se pudo, decilo "
                        "explícitamente en vez de prometerlo."
                    ),
                )
        return GuardrailResult(passed=True)


async def _emitir_cargo_pagado(
    session, cargo: SubscriptionCharge, sub: Subscription, provider_reference: str, *, duplicado: bool  # type: ignore[no-untyped-def]
) -> None:
    """`subscription.charge_paid` por el outbox, en la misma transacción que el cargo pagado
    (HU-SH-04, ADR-0123). Trae el período con sus dos bordes y la variante: si la
    suscripción es la de un negocio aprovisionado por un vendedor, el core aplica ese período
    al plan del negocio (fuente única). Para el resto de los negocios no hace nada."""
    from core.events.outbox import emit_domain
    from core.shared.events import ON_SUBSCRIPTION_CHARGE_PAID

    inicio = cargo.fecha_vencimiento or datetime.now(UTC)
    fin = sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla)
    clave = f"sub_charge_paid:{cargo.id}" + (f":dup:{provider_reference}" if duplicado else "")
    await emit_domain(
        session,
        tenant_id=cargo.tenant_id,
        topic=ON_SUBSCRIPTION_CHARGE_PAID,
        dedupe_key=clave,
        payload={
            "charge_id": str(cargo.id),
            "subscription_id": str(sub.id),
            "customer_id": str(sub.customer_id),
            "periodo": cargo.periodo,
            "periodo_inicio": inicio.isoformat(),
            "periodo_fin": fin.isoformat(),
            "dia_ancla": sub.dia_ancla,
            "monto": str(cargo.monto),
            "moneda": sub.moneda,
            "variant_id": str(sub.variante_id) if sub.variante_id else None,
            "plan_id": str(sub.plan_id) if sub.plan_id else None,
            "provider_reference": provider_reference,
            "pago_duplicado": duplicado,
        },
    )


async def _alertar_pago_sin_aplicar(tenant_id: uuid.UUID, cargo: SubscriptionCharge, motivo: str) -> None:
    """Llegó plata por un cargo que no corresponde aplicar: se registra y se avisa a los
    dueños para resolverlo a mano (devolución o cambio manual)."""
    from core.shared.audit import record_audit

    log.warning("subs_pago_sin_aplicar", charge_id=str(cargo.id), motivo=motivo)
    await record_audit(
        actor=f"plugin:{PLUGIN_ID}",
        action="subs_pago_sin_aplicar",
        output_data={"charge_id": str(cargo.id), "tipo": cargo.tipo, "monto": str(cargo.monto), "motivo": motivo},
    )
    from core.notifications.service import notify_standalone
    from core.notifications.spec import NotificationSpec

    await notify_standalone(
        tenant_id,
        NotificationSpec(
            event_key="subscriptions.pago_sin_aplicar",
            category="cobros",
            severity="warning",
            title="Pago de suscripción sin aplicar",
            body=f"Llegó el pago de {cargo.monto} ({cargo.periodo}) pero no se aplicó: {motivo}. Revísalo.",
            dedupe_key=f"subs_pago_sin_aplicar:{cargo.id}",
            section="pack_subscriptions",
            data={"kind": "subs_pago_sin_aplicar", "charge_id": str(cargo.id)},
            target={"entity": "cobro", "id": str(cargo.id), "section": "pack_subscriptions"},
        ),
    )


async def _alertar_monto_distinto(
    tenant_id: uuid.UUID | None, cargo: SubscriptionCharge, recibido: float, provider_reference: str
) -> None:
    """Pago con monto distinto al del cargo: no se marca pagado y se avisa al negocio."""
    from core.shared.audit import record_audit

    await record_audit(
        actor=f"plugin:{PLUGIN_ID}",
        action="subs_pago_monto_distinto",
        output_data={
            "charge_id": str(cargo.id),
            "periodo": cargo.periodo,
            "esperado": str(cargo.monto),
            "recibido": str(recibido),
            "provider_reference": provider_reference,
        },
    )
    if tenant_id is None:
        return
    from core.notifications.service import notify_standalone
    from core.notifications.spec import NotificationSpec

    await notify_standalone(
        tenant_id,
        NotificationSpec(
            event_key="subscriptions.pago_monto_distinto",
            category="cobros",
            severity="warning",
            title="Pago de suscripción con monto distinto",
            body=(
                f"Llegó un pago de {recibido} para el período {cargo.periodo}, pero el cargo es de "
                f"{cargo.monto}. No se marcó como pagado: revísalo."
            ),
            dedupe_key=f"subs_pago_monto_distinto:{cargo.id}:{provider_reference}",
            section="pack_subscriptions",
            data={"kind": "subs_pago_monto_distinto", "charge_id": str(cargo.id)},
            target={"entity": "cobro", "id": str(cargo.id), "section": "pack_subscriptions"},
        ),
    )
