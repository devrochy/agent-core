"""Próxima acción y fecha de seguimiento del lead (HU-CRM-21, ADR-0181 enmienda).

Una persona deja anotado qué va a hacer con un lead y cuándo (`Lead.next_action_note`,
`Lead.next_action_at`). La fecha se guarda siempre en UTC; «hoy», «mañana» y el texto que escribe
quien programa se resuelven con la zona horaria del negocio (`AppSettings.timezone`), no con la del
servidor ni la del navegador.

Qué cuenta como historia: el seguimiento vigente vive en el lead; cuando deja de estar vigente
(se hizo, se movió de fecha o se canceló) queda una fila en `pack_commerce_lead_followups` con su
nota, la fecha que tenía y cómo terminó. Cambiar solo la nota sin tocar la fecha no es otro seguimiento y
no deja fila.

Programar un seguimiento NO mueve `last_activity_at` (ADR-0149: solo un cambio de etapa lo mueve):
anotar «llamar el viernes» no puede dejar al lead en el reenganche automático de `jobs.py`.

El agente no escribe aquí (esta HU no agrega tools): solo lo hace una persona desde la ficha.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, and_, select

from core.db.models import AppSettings
from core.llm.prompt_builder import DEFAULT_TIMEZONE
from pack_commerce.models import Lead, LeadFollowup

NOTA_MAX = 200
DIAS_PROXIMOS = 7

# (valor del filtro, etiqueta). Cada lead cae en exactamente una opción, para que las pestañas sumen:
# vencido (ya pasó), hoy (resto del día del negocio), próximos (mañana hasta 7 días), más adelante
# (después de eso) y sin seguimiento (el lead no tiene fecha).
OPCIONES: tuple[tuple[str, str], ...] = (
    ("vencidos", "Vencidos"),
    ("hoy", "Hoy"),
    ("proximos_7_dias", "Próximos 7 días"),
    ("mas_adelante", "Más adelante"),
    ("sin_seguimiento", "Sin seguimiento"),
)
Estado = Literal["vencidos", "hoy", "proximos_7_dias", "mas_adelante"]
Resultado = Literal["hecho", "reprogramado", "cancelado"]


@dataclass(frozen=True)
class Limites:
    """Los cortes del día del negocio, en UTC, para clasificar un seguimiento."""

    ahora: datetime
    fin_de_hoy: datetime  # medianoche local de mañana
    fin_de_proximos: datetime  # medianoche local del día 8 contando hoy como 0


async def zona_del_negocio(session: Any, tenant_id: Any) -> ZoneInfo:
    """La zona del negocio; si falta o es inválida, la misma por defecto que usa el prompt."""
    nombre = (
        await session.execute(
            select(AppSettings.timezone).where(AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    try:
        return ZoneInfo(nombre or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _medianoche(dia_local: Any, zona: ZoneInfo) -> datetime:
    return datetime.combine(dia_local, time.min, tzinfo=zona).astimezone(UTC)


def limites(zona: ZoneInfo, ahora: datetime | None = None) -> Limites:
    """Cortes para `ahora` (UTC) en la zona del negocio. Se arman con fechas locales (no sumando 24 h)
    para que un cambio de hora no corra el día."""
    ahora = (ahora or datetime.now(UTC)).astimezone(UTC)
    hoy = ahora.astimezone(zona).date()
    return Limites(
        ahora=ahora,
        fin_de_hoy=_medianoche(hoy + timedelta(days=1), zona),
        fin_de_proximos=_medianoche(hoy + timedelta(days=DIAS_PROXIMOS + 1), zona),
    )


def estado(en: datetime | None, lim: Limites) -> Estado | None:
    """En qué opción cae un seguimiento; `None` si el lead no tiene fecha."""
    if en is None:
        return None
    if en < lim.ahora:
        return "vencidos"
    if en < lim.fin_de_hoy:
        return "hoy"
    if en < lim.fin_de_proximos:
        return "proximos_7_dias"
    return "mas_adelante"


def condicion(valor: str, columna: Any, lim: Limites) -> ColumnElement[bool]:
    """El filtro de la lista como condición sobre la columna `next_action_at` (comparada en UTC, sin
    `AT TIME ZONE`, para que el índice parcial sirva)."""
    if valor == "sin_seguimiento":
        return columna.is_(None)
    if valor == "vencidos":
        return columna < lim.ahora
    if valor == "hoy":
        return and_(columna >= lim.ahora, columna < lim.fin_de_hoy)
    if valor == "proximos_7_dias":
        return and_(columna >= lim.fin_de_hoy, columna < lim.fin_de_proximos)
    if valor == "mas_adelante":
        return columna >= lim.fin_de_proximos
    raise ValueError(f"Valor de seguimiento desconocido: {valor!r}")


def parsear_fecha(valor: str, zona: ZoneInfo) -> datetime:
    """La fecha que escribe una persona. Sin zona («2026-11-03T09:00», lo que da un selector de
    fecha y hora) es hora del negocio; con zona se respeta. Devuelve UTC."""
    try:
        fecha = datetime.fromisoformat(valor.strip())
    except ValueError as exc:
        raise ValueError("La fecha del seguimiento no es válida.") from exc
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=zona)
    return fecha.astimezone(UTC)


def texto_local(en: datetime | None, zona: ZoneInfo) -> str:
    """La fecha como la ve el negocio, sin zona (`2026-11-03T09:00`), lista para un selector."""
    return en.astimezone(zona).strftime("%Y-%m-%dT%H:%M") if en else ""


def _nota(valor: str | None) -> str | None:
    nota = (valor or "").strip()
    if len(nota) > NOTA_MAX:
        raise ValueError(f"La nota del seguimiento admite hasta {NOTA_MAX} caracteres.")
    return nota or None


def _cerrar(session: Any, lead: Lead, resultado: Resultado) -> None:
    """Guarda el seguimiento vigente del lead en la historia (si lo hay)."""
    if lead.next_action_at is None:
        return
    session.add(
        LeadFollowup(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            customer_id=lead.customer_id,
            note=lead.next_action_note,
            due_at=lead.next_action_at,
            outcome=resultado,
            actor="persona",
            closed_at=datetime.now(UTC),
        )
    )


def programar(
    session: Any,
    lead: Lead,
    *,
    fecha: datetime | None,
    nota: str | None,
    resultado: Resultado | None = None,
) -> None:
    """Deja el seguimiento del lead en (`fecha`, `nota`); `fecha=None` lo quita.

    El vigente pasa a la historia si cambia la fecha («reprogramado») o se quita («cancelado»), o
    con el `resultado` que diga quien llama («hecho»). Una nota sin fecha no tiene sentido y se
    rechaza. Lanza `ValueError` con el mensaje que ve la persona."""
    texto = _nota(nota)
    if fecha is None and texto is not None:
        raise ValueError("Elige la fecha del seguimiento.")
    previa = lead.next_action_at
    if previa is not None and (resultado is not None or fecha != previa):
        _cerrar(session, lead, resultado or ("cancelado" if fecha is None else "reprogramado"))
    lead.next_action_at = fecha
    lead.next_action_note = texto if fecha is not None else None


def completar(
    session: Any, lead: Lead, *, siguiente_fecha: datetime | None = None, siguiente_nota: str | None = None
) -> None:
    """Marca como hecho el seguimiento vigente y, en el mismo gesto, programa el siguiente si se pide."""
    if lead.next_action_at is None:
        raise ValueError("Este lead no tiene un seguimiento pendiente.")
    programar(session, lead, fecha=siguiente_fecha, nota=siguiente_nota, resultado="hecho")
