"""Escenarios de Suscripciones de los datos de prueba (HU-QA-13, adenda de ADR-0135 §8).

- `planes-de-suscripcion`: dos planes nuevos en el catálogo del pack (con el sufijo del caso en el
  código) y su snapshot, que el core le pasa al escenario `suscripciones`. El contenido sale de
  `presets/escenarios.json`.
- `suscripciones`: sobre esos planes y los clientes que creó el core, deja cuatro suscripciones: una activa al día y pagada,
una por vencer con su cargo pendiente en dos días, una vencida con un cargo de hace diez días sin
pagar y una anual pagada. Usa las funciones del pack (`crear_suscripcion_para`,
`registrar_pago_externo`); los estados «por vencer» y «vencida» se fijan con sus cargos, como los
deja el cron.

Sin efectos fuera del negocio: los clientes no tienen teléfono ni conversación de WhatsApp, así
que ningún aviso de cobro le llega a nadie, y el pago se registra sin emitir `charge_paid`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from core.db.session import get_session
from core.qa.contrato_datos import (
    ContextoDeNegocio,
    EntradaDeEscenario,
    EscenarioNoDisponible,
    PlanDeEscenario,
    ResumenDeEscenario,
)
from core.shared.tenant import get_current_tenant_id
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import etiqueta_periodo, sumar_periodo

# (índice del plan, estado final, días hasta el vencimiento del cargo pendiente o None si está al día)
CASOS: tuple[tuple[int, str, int | None], ...] = ((0, "activa", None), (0, "por_vencer", 2), (0, "vencida", -10), (1, "activa", None))


async def _cargo_pendiente(sub_id: uuid.UUID, vence: datetime, estado: str) -> None:
    async with get_session() as session:
        sub = (
            await session.execute(
                select(Subscription)
                .where(Subscription.tenant_id == get_current_tenant_id(), Subscription.id == sub_id)
                .with_for_update()
            )
        ).scalar_one()
        session.add(
            SubscriptionCharge(
                subscription_id=sub.id,
                periodo=etiqueta_periodo(vence, sub.periodicidad),
                monto=Decimal(sub.valor_periodo or 0),
                estado="pendiente",
                tipo="periodo",
                fecha_vencimiento=vence,
            )
        )
        # El cargo del período ya existe: el cron no lo vuelve a crear ni avisa de nuevo.
        sub.proximo_cargo = sumar_periodo(vence, sub.periodicidad, sub.dia_ancla)
        if estado == "vencida":
            sub.estado = "vencida"


async def sembrar(tenant_id: uuid.UUID, entrada: EntradaDeEscenario) -> ResumenDeEscenario:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("escenario fuera del tenant_scope del negocio")
    if len(entrada.planes) < 2 or len(entrada.clientes) < len(CASOS):
        raise EscenarioNoDisponible("El escenario necesita dos planes y cuatro clientes.")
    capacidad = SubscriptionCapabilityImpl()
    ahora = datetime.now(UTC)
    lineas: list[str] = []
    pagos = cargos = 0
    for n, ((indice, estado, dias), cliente) in enumerate(zip(CASOS, entrada.clientes, strict=False)):
        plan = entrada.planes[indice]
        sub = await crear_suscripcion_para(customer_id=cliente.customer_id, plan_id=plan.plan_id)
        if dias is None:
            inicio = sub.periodo_actual_inicio or ahora
            await capacidad.registrar_pago_externo(
                sub.id,
                inicio=inicio,
                fin=sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla),
                referencia=f"QA-PAGO-{entrada.sufijo}-{n + 1}",
                canal="transferencia",
            )
            pagos += 1
            lineas.append(f"{cliente.nombre}: {plan.plan.get('nombre')} activa y pagada")
        else:
            await _cargo_pendiente(sub.id, ahora + timedelta(days=dias), estado)
            cargos += 1
            texto = "por vencer en 2 días" if estado == "por_vencer" else "vencida hace 10 días, sin pagar"
            lineas.append(f"{cliente.nombre}: {plan.plan.get('nombre')} {texto}")
    return ResumenDeEscenario(
        conteos={"suscripciones": len(CASOS), "pagos": pagos, "cargos_pendientes": cargos},
        detalle=lineas,
    )


async def planes_de_suscripcion(tenant_id: uuid.UUID, entrada: EntradaDeEscenario) -> ResumenDeEscenario:
    from core.presets import load_fragment
    from pack_subscriptions.datos_de_prueba import PlanesDePrueba
    from pack_subscriptions.datos_de_prueba import sembrar as sembrar_datos

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("escenario fuera del tenant_scope del negocio")
    planes = PlanesDePrueba.model_validate(dict(load_fragment("pack_subscriptions", "escenarios").seed))
    contexto = ContextoDeNegocio(moneda=entrada.moneda, zona_horaria=entrada.zona_horaria, sufijo=entrada.sufijo)
    registros = await sembrar_datos(tenant_id, "planes", planes, contexto)
    salida = [
        PlanDeEscenario(plan_id=r.ref, plan=dict(r.metadata["plan"]))  # type: ignore[arg-type,index]
        for r in registros
        if r.kind == "plan" and r.metadata and r.metadata.get("plan")
    ]
    if len(salida) < 2:
        raise EscenarioNoDisponible("No se pudieron crear los planes de prueba.")
    return ResumenDeEscenario(
        conteos={"planes": len(salida)},
        detalle=["Planes " + " y ".join(f"{p.plan['nombre']} ({p.plan['periodicidad']})" for p in salida)],
        planes=salida,
    )
