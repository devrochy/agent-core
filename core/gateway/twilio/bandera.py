"""Bandera por negocio de los canales que se abren de a uno (ADR-0186 §12): APAGADA por defecto.

Vive en `tenants.settings["canales"] = {"whatsapp_twilio": bool, ...}`: clave propia, fuera de `agent_os` (ese
grupo del registro de configuración depende de `aos_runtime = v3`), mismo patrón que `avisos_bandera`. Con la
bandera apagada, el tipo de conexión del canal sigue «Próximamente» en el catálogo del negocio y su alta responde
404. Solo la cambia el master: `fijar` es el ÚNICO escritor y lo llama la ruta de plataforma master-only
(`/admin/platform/tenants/{id}/canales/{canal}`); una prueba lo vigila. Un valor desconocido cuenta como apagado.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

CLAVE = "canales"
ACCION_AUDITORIA = "canal_bandera"
# Canales que se abren por negocio. Una bandera fuera de esta lista no se puede fijar.
CANALES = frozenset({"whatsapp_twilio"})


class NegocioNoEncontrado(LookupError):
    pass


class CanalDesconocido(ValueError):
    pass


def _validar(canal: str) -> str:
    if canal not in CANALES:
        raise CanalDesconocido(canal)
    return canal


def habilitado_desde(settings: object, canal: str) -> bool:
    """¿El canal está abierto en este `tenants.settings`? Solo el literal `true` enciende."""
    bloque = settings.get(CLAVE) if isinstance(settings, dict) else None
    return isinstance(bloque, dict) and bloque.get(canal) is True


async def settings_del_negocio(session: AsyncSession, tenant_id: uuid.UUID) -> dict | None:
    """`tenants.settings` del negocio del `tenant_scope` en curso (`None` si no existe o está borrado)."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("bandera de canal: fuera del tenant_scope del negocio")
    fila = (
        await session.execute(
            text("SELECT settings FROM tenants WHERE id = :t AND deleted_at IS NULL"),
            {"t": tenant_id},
        )
    ).first()
    if fila is None:
        return None
    return fila[0] if isinstance(fila[0], dict) else {}


async def habilitado(session: AsyncSession, tenant_id: uuid.UUID, canal: str) -> bool:
    """Lectura barata dentro de la sesión de quien llama: apagado si el negocio no existe o está borrado.
    `tenants` no tiene RLS, así que solo se lee el del `tenant_scope` en curso."""
    settings = await settings_del_negocio(session, tenant_id)
    return settings is not None and habilitado_desde(settings, _validar(canal))


async def leer(tenant_id: uuid.UUID, canal: str) -> bool:
    """Para la ruta de plataforma: un negocio que no existe o está borrado es `NegocioNoEncontrado` (404)."""
    from core.db.session import get_session

    _validar(canal)
    async with get_session() as session:
        settings = await settings_del_negocio(session, tenant_id)
    if settings is None:
        raise NegocioNoEncontrado(str(tenant_id))
    return habilitado_desde(settings, canal)


async def fijar(tenant_id: uuid.UUID, canal: str, activo: bool, *, actor: str) -> bool:
    """Abre o cierra el canal con un UPDATE atómico que fusiona `settings.canales` (no pisa los otros canales ni
    el resto de `settings`) y lo audita como `canal_bandera`. Quien llama es la ruta master-only, dentro del
    `tenant_scope` del negocio. Devuelve el valor resultante."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    _validar(canal)
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("bandera de canal: fuera del tenant_scope del negocio")
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{canales}', "
                    "coalesce(settings->'canales', '{}'::jsonb) || cast(:cambio as jsonb)) "
                    "WHERE id = :t AND deleted_at IS NULL RETURNING id"
                ),
                {"t": tenant_id, "cambio": json.dumps({canal: bool(activo)})},
            )
        ).first()
        if fila is None:
            raise NegocioNoEncontrado(str(tenant_id))
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor[:120],
                action=ACCION_AUDITORIA,
                input={"canal": canal, "habilitado": bool(activo)},
            )
        )
    log.info(ACCION_AUDITORIA, tenant_id=str(tenant_id), canal=canal, habilitado=bool(activo))
    return bool(activo)
