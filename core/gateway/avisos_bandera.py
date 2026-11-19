"""Bandera por negocio de los avisos por evento por canal de texto (ADR-0178 D2, ADR-0108): APAGADA por defecto.

Con la bandera apagada, `send_event_notice` y los handlers de avisos se comportan EXACTAMENTE como antes (solo
conversaciones de `whatsapp`, plantilla de Meta o texto según la ventana). Encendida, un aviso también puede salir
como texto por QR, chat web, Messenger o Instagram, con el texto que el negocio definió para ese canal
(`message_templates_map[<evento>]["fallback_text_by_channel"]`).

Vive en `tenants.settings["avisos_por_canal"] = {"habilitada": bool}`: clave propia, mismo patrón que
`kernel/opciones/bandera.py` (ver ahí por qué es seguro con el jsonb de `settings`). Solo la cambia el master: `fijar`
es el ÚNICO escritor y lo llama la ruta de plataforma master-only (`/admin/platform/tenants/{id}/avisos-por-canal`);
una prueba (`test_avisos_bandera_guardia`) lo vigila. Un valor desconocido cuenta como apagado (fail-closed).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

CLAVE = "avisos_por_canal"
ACCION_AUDITORIA = "avisos_por_canal_bandera"


class NegocioNoEncontrado(LookupError):
    pass


def habilitada_desde(settings: object) -> bool:
    """¿La bandera está encendida en este `tenants.settings`? Solo el literal `true` enciende."""
    bloque = settings.get(CLAVE) if isinstance(settings, dict) else None
    return isinstance(bloque, dict) and bloque.get("habilitada") is True


async def habilitada(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Lectura barata: apagada si el negocio no existe o está borrado. `tenants` no tiene RLS, así que solo se lee
    el del `tenant_scope` en curso: otro id es un error de quien llama."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("avisos por canal: fuera del tenant_scope del negocio")
    fila = (
        await session.execute(
            text("SELECT settings FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
        )
    ).first()
    return fila is not None and habilitada_desde(fila[0])


async def habilitada_actual() -> bool:
    """La bandera del negocio del `tenant_scope` en curso. Fail-closed: sin negocio, o si no se puede leer, apagada
    (un aviso no debe caerse ni cambiar de vía por un error al leer la bandera)."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return False
    try:
        from core.db.session import get_session

        async with get_session() as session:
            return await habilitada(session, tenant_id)
    except Exception:
        log.warning("avisos_por_canal_bandera_ilegible", exc_info=True)
        return False


async def leer(tenant_id: uuid.UUID) -> bool:
    """Valor de la bandera para la ruta de plataforma: un negocio que no existe o está borrado es
    `NegocioNoEncontrado` (404), no «apagada». Dentro del `tenant_scope` del negocio."""
    from core.db.session import get_session

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("avisos por canal: fuera del tenant_scope del negocio")
    async with get_session() as session:
        fila = (
            await session.execute(
                text("SELECT settings FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
            )
        ).first()
    if fila is None:
        raise NegocioNoEncontrado(str(tenant_id))
    return habilitada_desde(fila[0])


async def fijar(tenant_id: uuid.UUID, activa: bool, *, actor: str) -> bool:
    """Enciende o apaga la bandera con un UPDATE atómico (`jsonb_set`, no pisa el resto de `settings`) y la audita
    como `avisos_por_canal_bandera`. Quien llama debe ser la ruta master-only y estar dentro del `tenant_scope`
    del negocio. Devuelve el valor resultante."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("avisos por canal: fuera del tenant_scope del negocio")
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{avisos_por_canal}', "
                    "cast(:valor as jsonb)) WHERE id = :t AND deleted_at IS NULL "
                    "RETURNING settings->'avisos_por_canal'->'habilitada'"
                ),
                {"t": tenant_id, "valor": json.dumps({"habilitada": bool(activa)})},
            )
        ).first()
        if fila is None:
            raise NegocioNoEncontrado(str(tenant_id))
        detalle: dict[str, Any] = {"habilitada": bool(activa)}
        session.add(AuditLog(tenant_id=tenant_id, actor=actor[:120], action=ACCION_AUDITORIA, input=detalle))
    log.info(ACCION_AUDITORIA, tenant_id=str(tenant_id), habilitada=bool(activa))
    return bool(activa)
