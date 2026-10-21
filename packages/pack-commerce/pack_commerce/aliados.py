"""Módulo de aliados por negocio (HU-CRM-03, D-SPEC-33-2).

Los aliados (proveedores) son opcionales: el master los enciende negocio por negocio desde
Negocios › Acciones. Con el módulo apagado la API de aliados responde 404 y el resto del
pack se comporta como antes.

La bandera vive en `tenants.settings["commerce_aliados"]`, como el interruptor del alta
automática (`core/provisioning/interruptor.py`): `tenants` es tabla de sistema, la escribe
solo el master por su endpoint (nunca un admin del negocio) y no hace falta redesplegar.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from core.db.models import Tenant
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

CLAVE = "commerce_aliados"


async def estado(tenant_id: uuid.UUID) -> dict[str, Any] | None:
    """`{"habilitado", "por", "en"}`, o `None` si el negocio no existe. Por defecto, apagado."""
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        return None
    guardado = dict((tenant.settings or {}).get(CLAVE) or {})
    return {
        "habilitado": bool(guardado.get("habilitado")),
        "por": guardado.get("por"),
        "en": guardado.get("en"),
    }


async def habilitados(tenant_id: uuid.UUID | None) -> bool:
    if tenant_id is None:
        return False
    actual = await estado(tenant_id)
    return bool(actual and actual["habilitado"])


async def fijar(tenant_id: uuid.UUID, *, habilitar: bool, por: str) -> dict[str, Any]:
    """Enciende o apaga los aliados del negocio. Queda en su `audit_log`.

    UPDATE atómico de solo esta clave: no pisa otras claves de `settings` escritas a la vez.
    Apagar no borra aliados ni `ally_id`: al volver a encenderlo, todo sigue ahí."""
    valor = {
        "habilitado": habilitar,
        "por": (por or "desconocido")[:254],
        "en": datetime.now(UTC).isoformat(),
    }
    async with get_session() as session:
        actualizadas = (
            await session.execute(
                text(
                    "update tenants set settings = coalesce(settings, '{}'::jsonb) "
                    "|| jsonb_build_object(cast(:clave as text), cast(:valor as jsonb)), updated_at = now() "
                    "where id = :id and deleted_at is null"
                ),
                {"clave": CLAVE, "valor": json.dumps(valor), "id": tenant_id},
            )
        ).rowcount
    if not actualizadas:
        raise LookupError("El negocio no existe.")
    async with tenant_scope(tenant_id):
        await record_audit(
            actor=valor["por"],
            action="aliados_habilitados" if habilitar else "aliados_deshabilitados",
            input_data={},
        )
    log.info("commerce_aliados_bandera", tenant_id=str(tenant_id), habilitado=habilitar)
    return valor


__all__ = ["CLAVE", "estado", "fijar", "habilitados"]
