"""Horario legal de los avisos de cobro (HU-SUB-18). Lógica pura, sin BD.

La Ley 2300 de 2023 (art. 3) permite contactar por cobranza de lunes a viernes de
7:00 a 19:00 y los sábados de 8:00 a 15:00, nunca domingos ni festivos, y no más de
una vez el mismo día. La Superintendencia Financiera aplica las mismas reglas a los
mensajes publicitarios. La ventana se evalúa en la zona horaria del negocio
(ADR-0091). Frecuencia y alcance exactos: por verificar con el abogado (HU-GTM-02).

Los festivos de Colombia se calculan con la regla de la Ley 51 de 1983 ("ley
Emiliani"): fijos, trasladados al lunes siguiente, y los que dependen de la Pascua.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Mismo formato que `business_hours` (core/orchestrator/escalation.py): day = weekday()
# (0 = lunes ... 6 = domingo), horas "HH:MM", el `to` excluido.
VENTANA_LEGAL_CO: tuple[dict[str, object], ...] = (
    *({"day": d, "from": "07:00", "to": "19:00"} for d in range(5)),
    {"day": 5, "from": "08:00", "to": "15:00"},
)

ZONA_POR_DEFECTO = "America/Bogota"


def _pascua(anio: int) -> date:
    """Domingo de Pascua (algoritmo anónimo gregoriano)."""
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    mes, dia = divmod(h + m - 7 * n + 114, 31)
    return date(anio, mes, dia + 1)


def _al_lunes(dia: date) -> date:
    return dia + timedelta(days=(7 - dia.weekday()) % 7)


@cache
def festivos_co(anio: int) -> frozenset[date]:
    """Festivos nacionales de Colombia en `anio`."""
    fijos = [date(anio, 1, 1), date(anio, 5, 1), date(anio, 7, 20), date(anio, 8, 7),
             date(anio, 12, 8), date(anio, 12, 25)]
    trasladables = [date(anio, 1, 6), date(anio, 3, 19), date(anio, 6, 29), date(anio, 8, 15),
                    date(anio, 10, 12), date(anio, 11, 1), date(anio, 11, 11)]
    pascua = _pascua(anio)
    semana_santa = [pascua - timedelta(days=3), pascua - timedelta(days=2)]
    de_pascua = [pascua + timedelta(days=d) for d in (39, 60, 68)]  # Ascensión, Corpus, Sagrado Corazón
    return frozenset(fijos + semana_santa + [_al_lunes(d) for d in trasladables + de_pascua])


def es_festivo(dia: date, pais: str = "CO") -> bool:
    if pais != "CO":
        return False  # solo Colombia por ahora; otro país = sin festivos conocidos
    return dia in festivos_co(dia.year)


def zona(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or ZONA_POR_DEFECTO)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(ZONA_POR_DEFECTO)


def hora_local(now: datetime, tz: str | None) -> datetime:
    return now.astimezone(zona(tz))


def en_horario_de_cobro(
    now: datetime,
    tz: str | None,
    ventana: list[dict[str, object]] | tuple[dict[str, object], ...] = VENTANA_LEGAL_CO,
    pais: str = "CO",
) -> bool:
    """True si `now` cae dentro de la ventana permitida, en la zona del negocio, y no
    es domingo ni festivo. Una ventana vacía no permite nada (falla cerrado)."""
    local = hora_local(now, tz)
    if local.weekday() == 6 or es_festivo(local.date(), pais):
        return False
    hhmm = local.strftime("%H:%M")
    return any(
        rango["day"] == local.weekday() and str(rango["from"]) <= hhmm < str(rango["to"])
        for rango in ventana
    )


def proximo_inicio_de_cobro(
    now: datetime,
    tz: str | None,
    ventana: list[dict[str, object]] | tuple[dict[str, object], ...] = VENTANA_LEGAL_CO,
    pais: str = "CO",
) -> datetime | None:
    """`now` si está dentro del horario de cobro; si no, el próximo instante en que abre
    (en la misma zona que `now`). None si la ventana no abre en los próximos 15 días."""
    if en_horario_de_cobro(now, tz, ventana, pais):
        return now
    local = hora_local(now, tz)
    for desplazamiento in range(16):
        dia = local.date() + timedelta(days=desplazamiento)
        if dia.weekday() == 6 or es_festivo(dia, pais):
            continue
        aperturas = sorted(str(r["from"]) for r in ventana if r["day"] == dia.weekday())
        for desde in aperturas:
            hh, mm = (int(x) for x in desde.split(":"))
            inicio = datetime(dia.year, dia.month, dia.day, hh, mm, tzinfo=local.tzinfo)
            if inicio > local:
                return inicio.astimezone(now.tzinfo)
    return None


def siguiente_dia(momento: datetime, tz: str | None) -> datetime:
    """Medianoche local del día siguiente a `momento`."""
    local = hora_local(momento, tz)
    manana = local.date() + timedelta(days=1)
    return datetime(manana.year, manana.month, manana.day, tzinfo=local.tzinfo).astimezone(momento.tzinfo)


def dentro_de_lo_legal(ventana: list[dict[str, object]], pais: str = "CO") -> bool:
    """True si cada rango de `ventana` cabe dentro de la ventana legal del país: el
    negocio puede achicar el horario, nunca ampliarlo."""
    legal = {int(r["day"]): (str(r["from"]), str(r["to"])) for r in VENTANA_LEGAL_CO}  # type: ignore[call-overload]
    for rango in ventana:
        dia = int(rango["day"])  # type: ignore[call-overload]
        if dia not in legal:
            return False
        desde, hasta = legal[dia]
        if not (desde <= str(rango["from"]) < str(rango["to"]) <= hasta):
            return False
    return True
