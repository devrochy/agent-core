"""Banderas del Agent OS por negocio (ADR-0112): `tenants.aos_runtime` y `tenants.aos_procedures`.

Las guarda la base (migración 0070, con el CHECK de orden `aos_procedures ⇒ aos_runtime = 'v3'`)
y solo las edita el master. Se leen en cada turno, así un cambio surte efecto sin redeploy.
Un solo lector para todo el kernel: no se consultan `tenants` sueltas en cada módulo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class BanderasAOS:
    runtime: str = "legacy"   # legacy | v3
    procedimientos: bool = False


APAGADAS = BanderasAOS()


async def banderas_del_negocio(session: AsyncSession, tenant_id: uuid.UUID) -> BanderasAOS:
    """Banderas del negocio; apagadas si el negocio no existe (fail-closed hacia lo de siempre)."""
    fila = (
        await session.execute(
            text("SELECT aos_runtime, aos_procedures FROM tenants WHERE id = :t"), {"t": tenant_id}
        )
    ).first()
    if fila is None:
        return APAGADAS
    return BanderasAOS(runtime=fila[0], procedimientos=bool(fila[1]))
