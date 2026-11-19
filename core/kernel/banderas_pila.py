"""Banderas de la pila de tareas (HU-AOS-20, adenda de ADR-0116); el encargo y la consulta de
HU-AOS-19 viven en `core.kernel.colaboracion`.

Viven en `tenants.settings.agent_os` y solo las cambia el master (`/admin/platform/tenants/{id}/
colaboracion`). El turno las lee con el resto de banderas en `banderas_del_negocio` (sin consulta
extra). Se entregan por partes: lo que todavía no está construido no se puede encender
(`DISPONIBLES`), para que nadie active algo que el runtime trataría como sombra sin decirlo.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from core.kernel.banderas import Colaboracion
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Lo que se puede encender (la HU completa: pila activa y verificación de horarios y preguntas).
DISPONIBLES: dict[str, tuple[str, ...]] = {
    "pila": ("off", "shadow", "active"),
    "verificar_horarios": ("shadow", "active"),
    "verificar_preguntas": ("shadow", "active"),
}


class NegocioNoEncontrado(LookupError):
    pass


class NoDisponible(ValueError):
    """Un valor que el runtime todavía no aplica (llega en una parte posterior de la HU)."""


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("colaboración: fuera del tenant_scope del negocio")


async def leer(tenant_id: uuid.UUID) -> Colaboracion:
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                text("SELECT settings->'agent_os' FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
            )
        ).first()
    if fila is None:
        raise NegocioNoEncontrado(str(tenant_id))
    return Colaboracion.desde(fila[0])


async def fijar(tenant_id: uuid.UUID, cambios: dict[str, str], *, actor: str) -> Colaboracion:
    """Cambia las banderas con un UPDATE atómico dentro de `settings.agent_os` (no pisa el resto de
    `settings`) y lo audita como `agent_os_colaboracion`. Un valor no disponible es `NoDisponible`."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    for clave, valor in cambios.items():
        if valor not in DISPONIBLES.get(clave, ()):
            raise NoDisponible(f"{clave} = {valor} todavía no está disponible")
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{agent_os}', "
                    "coalesce(settings->'agent_os', '{}'::jsonb) || cast(:cambios as jsonb)) "
                    "WHERE id = :t AND deleted_at IS NULL RETURNING settings->'agent_os'"
                ),
                {"t": tenant_id, "cambios": json.dumps(cambios)},
            )
        ).first()
        if fila is None:
            raise NegocioNoEncontrado(str(tenant_id))
        detalle: dict[str, Any] = {"cambios": cambios}
        session.add(AuditLog(tenant_id=tenant_id, actor=actor[:120], action="agent_os_colaboracion", input=detalle))
    log.info("agent_os_colaboracion", tenant_id=str(tenant_id), **cambios)
    return Colaboracion.desde(fila[0])


__all__ = ["DISPONIBLES", "NegocioNoEncontrado", "NoDisponible", "fijar", "leer"]
