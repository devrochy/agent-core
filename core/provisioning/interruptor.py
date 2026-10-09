"""Interruptor del alta automática (HU-SH-07, ADR-0125).

Ante un ataque, el master pausa el alta automática de un vendedor desde Plataforma (o con
`agent-core tenant interruptor-altas`). Con el alta pausada, las suscripciones activadas
siguen registrándose, pero cada alta nueva queda **en revisión** en vez de crear el negocio:
una persona la aprueba o la descarta.

El estado vive en `tenants.settings["aprovisionamiento"]` del vendedor: `tenants` es tabla de
sistema y solo la escriben el master y el core, nunca un admin del negocio. Una variable de
entorno (como la allowlist) obligaría a redesplegar en medio de un ataque.
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

CLAVE = "aprovisionamiento"
_MAX_MOTIVO = 300


async def estado(vendedor: uuid.UUID) -> dict[str, Any]:
    """`{"pausado": bool, "motivo", "por", "en"}`. Sin nada guardado, el alta está activa."""
    async with get_session() as session:
        tenant = await session.get(Tenant, vendedor)
    guardado = dict(((tenant.settings or {}) if tenant else {}).get(CLAVE) or {})
    return {
        "pausado": bool(guardado.get("pausado")),
        "motivo": guardado.get("motivo"),
        "por": guardado.get("por"),
        "en": guardado.get("en"),
    }


async def pausado(vendedor: uuid.UUID) -> bool:
    return (await estado(vendedor))["pausado"]


async def fijar(vendedor: uuid.UUID, *, pausar: bool, motivo: str | None, por: str) -> dict[str, Any]:
    """Pausa o reanuda el alta automática del vendedor. Queda en `audit_log` del vendedor.

    Escribe solo su clave con un UPDATE atómico: no pisa otras claves de `settings` que otro
    proceso esté escribiendo a la vez."""
    valor = {
        "pausado": pausar,
        "motivo": ((motivo or "").strip()[:_MAX_MOTIVO] or None),
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
                {"clave": CLAVE, "valor": json.dumps(valor), "id": vendedor},
            )
        ).rowcount
    if not actualizadas:
        raise LookupError("El negocio vendedor no existe.")
    async with tenant_scope(vendedor):
        await record_audit(
            actor=valor["por"],
            action="alta_automatica_pausada" if pausar else "alta_automatica_reanudada",
            input_data={"motivo": valor["motivo"]},
        )
    log.warning(
        "alta_automatica_interruptor",
        tenant_id=str(vendedor),
        pausado=pausar,
    )
    return valor


__all__ = ["CLAVE", "estado", "fijar", "pausado"]
