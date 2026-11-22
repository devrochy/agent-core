"""Cierre del ciclo de créditos: arrastre de abonos y acumulación del sobrante (ADR-0209 §3, HU-BILL-22).

Al cerrar un ciclo se reparte lo consumido con el mismo orden que el saldo (`creditos.repartir`: mes →
acumulado → recargas) y se inserta para el ciclo siguiente, en `credit_grants`:

- lo que queda de cada **recarga**, con su mismo `origen` (no vence);
- lo que queda de cada **acumulado** con `vence_en >= fin del ciclo siguiente`, con su `origen` y su
  `vence_en`;
- el **sobrante del mes** como `acumulado` nuevo si el plan acumula (`features.acumulable_meses > 0`) y
  la suscripción tenía un período pago: `origen` = inicio del ciclo que cierra y `vence_en` = fin del
  ciclo que cierra avanzado `acumulable_meses` períodos con `sumar_periodo` y el `dia_ancla`.

Lo consumido por encima del saldo (colchón) no se arrastra como deuda. Idempotente por la unicidad
`(tenant_id, ciclo, tipo, origen)` con `ON CONFLICT DO NOTHING`: reintentar no duplica. Lo que se
arrastra se trunca a la décima hacia abajo, así un reintento calcula exactamente las mismas filas.

Corre dentro del `tenant_scope` del negocio y en la transacción del llamador (no hace commit): el cron
(`core/tasks/billing_lifecycle.cerrar_ciclos_de_credito`) y el período reiniciado de
`service.aplicar_pago_de_periodo`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.billing.creditos import (
    creditos_que_quedan,
    leer_abonos,
    medir_creditos,
    repartir,
)
from core.billing.periodos import PERIODICIDAD
from core.shared.logger import get_logger
from core.subscriptions.periodos import sumar_periodo

log = get_logger(__name__)


@dataclass(frozen=True)
class CierreDeCiclo:
    medido: bool  # False: no había abonos ni nada que acumular (no se midió el ledger)
    insertados: int = 0
    recargas: Decimal = Decimal(0)
    acumulado: Decimal = Decimal(0)  # arrastrado + sobrante nuevo


def origen_del_ciclo(inicio: datetime) -> str:
    """`origen` del acumulado que nace en el ciclo `inicio`: el instante en UTC, ISO 8601."""
    return inicio.astimezone(UTC).isoformat()


def vencimiento_del_acumulado(fin_del_ciclo: datetime, meses: int, dia_ancla: int | None) -> datetime:
    """Fin del ciclo que cierra avanzado `meses` períodos por calendario (no un intervalo fijo)."""
    vence = fin_del_ciclo
    for _ in range(meses):
        vence = sumar_periodo(vence, PERIODICIDAD, dia_ancla)
    return vence


async def cerrar_ciclo(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    *,
    credito_mensual: int | None,
    acumulable_meses: int,
    inicio: datetime,
    hasta: datetime,
    siguiente_inicio: datetime,
    siguiente_fin: datetime,
    dia_ancla: int | None,
) -> CierreDeCiclo:
    """Cierra el ciclo `inicio` (consumo medido en `[inicio, hasta)`) y arrastra al ciclo `siguiente_inicio`.

    `hasta` es el fin de la ventana en el cron y el inicio del período nuevo cuando se reinicia
    (`aplicar_pago_de_periodo`); en los dos casos es también el «fin del ciclo que cierra» del que sale
    el `vence_en` del sobrante. `acumulable_meses` ya viene en 0 si el plan no acumula o la suscripción
    no tenía un período pago."""
    from core.db.models import CreditGrant

    abonos = await leer_abonos(session, tenant_id, inicio)  # exige el tenant_scope del negocio
    mensual = Decimal(credito_mensual or 0)
    if not abonos and (acumulable_meses <= 0 or mensual <= 0):
        return CierreDeCiclo(medido=False)
    usado = (await medir_creditos(session, tenant_id, desde=inicio, hasta=hasta)).total
    reparto = repartir(usado, mensual, abonos)

    filas: list[dict[str, object]] = []
    for uso in reparto.abonos:
        queda = creditos_que_quedan(uso.queda)
        if queda <= 0:
            continue
        abono = uso.abono
        if abono.tipo == "acumulado" and (abono.vence_en is None or abono.vence_en < siguiente_fin):
            continue  # vence antes de terminar el ciclo siguiente: se pierde
        filas.append({"tipo": abono.tipo, "creditos": queda, "origen": abono.origen, "vence_en": abono.vence_en})
    sobrante = creditos_que_quedan(reparto.queda_mes)
    if acumulable_meses > 0 and sobrante > 0:
        filas.append(
            {
                "tipo": "acumulado",
                "creditos": sobrante,
                "origen": origen_del_ciclo(inicio),
                "vence_en": vencimiento_del_acumulado(hasta, acumulable_meses, dia_ancla),
            }
        )
    if not filas:
        return CierreDeCiclo(medido=True)

    resultado = await session.execute(
        pg_insert(CreditGrant)
        .values([{"id": uuid.uuid4(), "tenant_id": tenant_id, "ciclo": siguiente_inicio, **f} for f in filas])
        .on_conflict_do_nothing(index_elements=["tenant_id", "ciclo", "tipo", "origen"])
    )
    insertados = int(resultado.rowcount or 0)
    recargas = sum((Decimal(str(f["creditos"])) for f in filas if f["tipo"] == "recarga"), Decimal(0))
    acumulado = sum((Decimal(str(f["creditos"])) for f in filas if f["tipo"] == "acumulado"), Decimal(0))
    if insertados:
        log.info(
            "billing_ciclo_cerrado",
            tenant_id=str(tenant_id),
            ciclo=inicio.isoformat(),
            siguiente=siguiente_inicio.isoformat(),
            insertados=insertados,
            recargas=float(recargas),
            acumulado=float(acumulado),
            colchon=float(reparto.sobre_el_saldo),
        )
    return CierreDeCiclo(medido=True, insertados=insertados, recargas=recargas, acumulado=acumulado)


def meses_que_acumula(plan_id: str | None, status: str | None) -> int:
    """`features.acumulable_meses` del plan si la suscripción tenía un período pago; si no, 0 (Free, Basic,
    prueba o cancelada: el sobrante del mes se pierde)."""
    from core.billing.periodos import tiene_periodo_pago
    from core.billing.plans import get_plan

    plan = get_plan(plan_id or "")
    if plan is None or not tiene_periodo_pago(plan_id, status):
        return 0
    try:
        return max(int(plan.features.get("acumulable_meses") or 0), 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CierreDeCiclo",
    "cerrar_ciclo",
    "meses_que_acumula",
    "origen_del_ciclo",
    "vencimiento_del_acumulado",
]
