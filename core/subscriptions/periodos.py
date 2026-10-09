"""Períodos por calendario de una suscripción (HU-SUB-04, SPEC-06 § Período por calendario).

- `mensual`: mismo día del mes siguiente; si ese día no existe, el último del mes.
- `trimestral`: +3 meses con la misma regla; `anual`: +1 año (29-feb → 28-feb).

El **día ancla** es el día del mes en que empezó la suscripción: un plan del 31 de enero
vence el 28/29 de febrero y vuelve al 31 en marzo (no se queda en el 28 para siempre).

Vive en el core desde HU-SH-04 (ADR-0123): el cobro de la plataforma (`core/billing`) y el
proveedor `pack_subscriptions` tienen que cortar los períodos en el mismo día, o el mismo
período tendría dos referencias. `pack_subscriptions.periodos` lo re-exporta.
"""

from __future__ import annotations

import calendar
from datetime import datetime

MESES = {"mensual": 1, "trimestral": 3, "anual": 12}
PERIODICIDADES = tuple(MESES)


def meses_de(periodicidad: str | None) -> int:
    return MESES.get((periodicidad or "mensual").strip().lower(), 1)


def sumar_periodo(desde: datetime, periodicidad: str | None, dia_ancla: int | None = None) -> datetime:
    """Inicio del período siguiente a `desde` (conserva hora y zona)."""
    total = desde.month - 1 + meses_de(periodicidad)
    anio = desde.year + total // 12
    mes = total % 12 + 1
    ancla = dia_ancla or desde.day
    dia = min(ancla, calendar.monthrange(anio, mes)[1])
    return desde.replace(year=anio, month=mes, day=dia, second=0, microsecond=0)


def etiqueta_periodo(inicio: datetime, periodicidad: str | None) -> str:
    """`AAAA-MM` (o `AAAA` en anual): la clave del cargo del período."""
    return inicio.strftime("%Y") if meses_de(periodicidad) == 12 else inicio.strftime("%Y-%m")


__all__ = ["MESES", "PERIODICIDADES", "etiqueta_periodo", "meses_de", "sumar_periodo"]
