"""El período de la plataforma y su referencia (HU-SH-04, ADR-0123).

Un período del plan de un negocio va de `inicio` a `fin` por **calendario** (mismo día del
mes siguiente, con día ancla), igual que en `pack_subscriptions`: así el canal asistente
(la suscripción del cliente en el negocio vendedor) y el panel (`/plan`) cortan el mismo
período el mismo día.

La **referencia** del período es `plat-<slug>-<AAAAMM>` (el mes en que empieza). Es la misma
en los dos canales y es la llave que impide cobrar dos veces el mismo período: un período se
aplica una sola vez a `tenant_subscriptions` (índice único de `subscription_payments`).
Dos períodos seguidos siempre empiezan en meses distintos, así que la referencia no se repite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from core.subscriptions.periodos import sumar_periodo

PERIODICIDAD = "mensual"
_REFERENCIA = re.compile(r"^plat-.+-(?P<anio>\d{4})(?P<mes>\d{2})$")
# Estados de `tenant_subscriptions` con un período pago en curso (o vencido sin pagar).
ESTADOS_CON_PERIODO_PAGO = ("active", "past_due")


@dataclass(frozen=True)
class Periodo:
    inicio: datetime
    fin: datetime
    referencia: str
    # Arranca hoy (venía de Free, de una prueba o de un período vencido hace mucho).
    desde_cero: bool = False

    @property
    def etiqueta(self) -> str:
        """`AAAA-MM`: la clave del cargo del mismo período en `pack_subscriptions`."""
        return self.inicio.strftime("%Y-%m")


def referencia_del_periodo(slug: str, inicio: datetime) -> str:
    return f"plat-{slug}-{inicio:%Y%m}"


def etiqueta_de_referencia(referencia: str) -> str | None:
    """`plat-<slug>-<AAAAMM>` → `AAAA-MM`, o None si no es una referencia de período."""
    coincide = _REFERENCIA.match(referencia or "")
    if coincide is None:
        return None
    return f"{coincide['anio']}-{coincide['mes']}"


def periodo_de(slug: str, inicio: datetime, dia_ancla: int | None = None, *, desde_cero: bool = False) -> Periodo:
    """El período que empieza en `inicio`."""
    return Periodo(
        inicio=inicio,
        fin=sumar_periodo(inicio, PERIODICIDAD, dia_ancla or inicio.day),
        referencia=referencia_del_periodo(slug, inicio),
        desde_cero=desde_cero,
    )


def tiene_periodo_pago(plan_id: str | None, status: str | None) -> bool:
    from core.billing.plans import get_plan

    plan = get_plan(plan_id or "")
    return plan is not None and not plan.is_free and status in ESTADOS_CON_PERIODO_PAGO


def periodo_a_cobrar(
    *,
    slug: str,
    plan_id: str | None,
    status: str | None,
    period_end: datetime | None,
    dia_ancla: int | None,
    now: datetime,
) -> Periodo:
    """El período que toca pagar ahora:

    - sin un plan pago en curso (Free, prueba, cancelada): uno nuevo desde hoy (D-16: un plan
      pago no suma los días que le quedaban a la prueba);
    - con el período al día: el siguiente (renovación anticipada);
    - vencido hace menos de un período: el vencido, que ya corre (pagarlo desbloquea hoy);
    - vencido hace más de un período: uno nuevo desde hoy (pagar uno ya terminado no
      desbloquearía el negocio).
    """
    if period_end is None or not tiene_periodo_pago(plan_id, status):
        return periodo_de(slug, now, now.day, desde_cero=True)
    ancla = dia_ancla or period_end.day
    if period_end > now:
        return periodo_de(slug, period_end, ancla)
    vencido = periodo_de(slug, period_end, ancla)
    if vencido.fin > now:
        return vencido
    return periodo_de(slug, now, now.day, desde_cero=True)


def ventana_mensual(period_start: datetime, dia_ancla: int | None, now: datetime) -> tuple[datetime, datetime]:
    """El mes de consumo que contiene `now`, alineado con el ciclo de la suscripción (ADR-0144).

    Arranca en `period_start` y avanza mes a mes por calendario (mismo día ancla que el cobro)
    hasta el tramo que contiene `now`. Cubre una renovación anticipada (`period_end` se alarga
    y `period_start` no se mueve), un cron atrasado y un período vencido: el crédito se reinicia
    cada mes aunque `period_start` no se haya movido. Con `now` antes de `period_start` devuelve
    el primer tramo."""
    ancla = dia_ancla or period_start.day
    inicio = period_start
    fin = sumar_periodo(inicio, PERIODICIDAD, ancla)
    # Tope defensivo (100 años): un `period_start` corrupto no deja el panel en un bucle.
    for _ in range(1200):
        if fin > now:
            break
        inicio, fin = fin, sumar_periodo(fin, PERIODICIDAD, ancla)
    return inicio, fin


__all__ = [
    "ESTADOS_CON_PERIODO_PAGO",
    "PERIODICIDAD",
    "Periodo",
    "etiqueta_de_referencia",
    "periodo_a_cobrar",
    "periodo_de",
    "referencia_del_periodo",
    "tiene_periodo_pago",
    "ventana_mensual",
]
