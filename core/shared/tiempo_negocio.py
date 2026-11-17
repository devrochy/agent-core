"""Reloj del negocio (ADR-0202): zona horaria y fecha/hora locales.

La base guarda instantes en UTC y las fechas de calendario en `date`. La lógica de negocio que
depende de «hoy» o de la hora local (mora, cierres, ventanas de envío) debe usar este módulo y no
`date.today()`/`datetime.now()`: esos usan la zona del servidor (UTC) y pueden caer un día antes
o después para un negocio de América.

Todo se lee dentro del `tenant_scope` en curso (`app_settings.timezone`), con
`America/Bogota` como respaldo si el negocio no tiene fila o la zona es inválida.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from core.db.models import AppSettings
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

ZONA_POR_DEFECTO = "America/Bogota"


def zona(nombre: str | None) -> ZoneInfo:
    """`ZoneInfo` de un nombre; un nombre inválido cae a la zona por defecto (nunca lanza)."""
    try:
        return ZoneInfo(nombre or ZONA_POR_DEFECTO)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(ZONA_POR_DEFECTO)


async def zona_del_negocio(tenant_id: uuid.UUID | None = None) -> ZoneInfo:
    """Zona horaria del negocio del `tenant_scope` (o del `tenant_id` que se pase)."""
    tid = tenant_id or get_current_tenant_id()
    if tid is None:
        return ZoneInfo(ZONA_POR_DEFECTO)
    async with get_session() as session:
        nombre = (
            await session.execute(
                select(AppSettings.timezone).where(
                    AppSettings.tenant_id == tid,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    return zona(nombre)


async def ahora_del_negocio(tenant_id: uuid.UUID | None = None) -> datetime:
    """Instante actual con la zona del negocio (aware, listo para mostrar o comparar)."""
    return datetime.now(UTC).astimezone(await zona_del_negocio(tenant_id))


async def hoy_del_negocio(tenant_id: uuid.UUID | None = None) -> date:
    """Día de calendario del negocio. Es lo que reemplaza a `date.today()`."""
    return (await ahora_del_negocio(tenant_id)).date()


def a_local(instante: datetime, zona_horaria: ZoneInfo) -> datetime:
    """UTC (o cualquier aware) → hora de pared del negocio."""
    return instante.astimezone(zona_horaria)


def a_utc(instante: datetime, zona_horaria: ZoneInfo) -> datetime:
    """Hora de pared del negocio → UTC. Un naive se interpreta en la zona del negocio."""
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=zona_horaria)
    return instante.astimezone(UTC)
