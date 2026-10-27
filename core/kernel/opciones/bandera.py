"""Bandera por negocio de las opciones directas (SPEC-40, ADR-0172 §5): APAGADA por defecto.

Vive en `tenants.settings["opciones_directas"] = {"habilitada": bool}`: clave propia, NO dentro de
`settings.agent_os`, porque el registro de opciones de configuración exige que toda bandera de ese grupo
dependa de `aos_runtime = v3` y el resolver de las opciones no depende de v3 ni de los procedimientos.

Solo la cambia el master: `fijar` es el ÚNICO escritor y lo llama la ruta de plataforma master-only del PR
de la API (como `banderas_pila.fijar` con `/colaboracion`); ningún router del dueño la escribe, y una
prueba (`test_nadie_fuera_del_modulo…`) lo vigila. Límite conocido (a diferencia de las `aos_*`, 0097, no hay función ni trigger en la base):
`tenants.settings` es un jsonb que `app_runtime` puede reescribir entero, y los caminos de leer-modificar-
escribir del dict completo (presets, suscripciones) podrían reponer un `true` que el master apagó. Con la
bandera apagada y sin consumidor es aceptable en este PR; si la ruta de plataforma lo pide, pasa a columna.
Un valor desconocido cuenta como apagado (fail-closed).
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

CLAVE = "opciones_directas"
ACCION_AUDITORIA = "opciones_directas_bandera"


class NegocioNoEncontrado(LookupError):
    pass


def habilitada_desde(settings: object) -> bool:
    """¿La bandera está encendida en este `tenants.settings`? Solo el literal `true` enciende."""
    bloque = settings.get(CLAVE) if isinstance(settings, dict) else None
    return isinstance(bloque, dict) and bloque.get("habilitada") is True


async def habilitada(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Lectura barata del consumer/API: apagada si el negocio no existe o está borrado. `tenants` no tiene RLS,
    así que solo se lee el del `tenant_scope` en curso: otro id es un error de quien llama."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("opciones directas: fuera del tenant_scope del negocio")
    fila = (
        await session.execute(
            text("SELECT settings FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
        )
    ).first()
    return fila is not None and habilitada_desde(fila[0])


async def fijar(tenant_id: uuid.UUID, activa: bool, *, actor: str) -> bool:
    """Enciende o apaga la bandera con un UPDATE atómico (`jsonb_set`, no pisa el resto de `settings`) y la
    audita como `opciones_directas_bandera`. Quien llama debe ser la ruta master-only y estar dentro del
    `tenant_scope` del negocio. Devuelve el valor resultante."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("opciones directas: fuera del tenant_scope del negocio")
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{opciones_directas}', "
                    "cast(:valor as jsonb)) WHERE id = :t AND deleted_at IS NULL "
                    "RETURNING settings->'opciones_directas'->'habilitada'"
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
