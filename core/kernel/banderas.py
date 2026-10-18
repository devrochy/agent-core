"""Banderas del Agent OS por negocio (ADR-0112): `tenants.aos_runtime`, `tenants.aos_procedures` y
`tenants.aos_routing` (HU-AOS-17, ADR-0138).

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
    # Enrutamiento entre especialistas (HU-AOS-17, migración 0094, ADR-0138): off | shadow | active.
    ruteo: str = "off"


APAGADAS = BanderasAOS()


async def banderas_del_negocio(session: AsyncSession, tenant_id: uuid.UUID) -> BanderasAOS:
    """Banderas del negocio; apagadas si el negocio no existe (fail-closed hacia lo de siempre)."""
    fila = (
        await session.execute(
            text("SELECT aos_runtime, aos_procedures, aos_routing FROM tenants WHERE id = :t"),
            {"t": tenant_id},
        )
    ).first()
    if fila is None:
        return APAGADAS
    return BanderasAOS(runtime=fila[0], procedimientos=bool(fila[1]), ruteo=str(fila[2] or "off"))


async def negocios_con_procedimientos(session: AsyncSession) -> list[uuid.UUID]:
    """Negocios vivos con `aos_procedures` encendida (crons del kernel, HU-AOS-07). `tenants` es
    tabla de sistema, sin RLS: cada negocio se recorre después bajo su `tenant_scope`."""
    filas = await session.execute(
        text("SELECT id FROM tenants WHERE aos_procedures AND deleted_at IS NULL ORDER BY id")
    )
    return [fila[0] for fila in filas]
