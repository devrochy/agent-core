"""Cron jobs de pack-cobranza.

- `marcar_mora`: pasa las cuotas vencidas a `atrasada` y actualiza el color de
  riesgo del cliente (blanco/azul/rojo). Corre de madrugada.

El decorador `@register_cron` salta automáticamente si el pack está inactivo.
El filtro por tenant habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text, update

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_cobranza.models import Cliente, Cuota, Prestamo

log = get_logger(__name__)

PACK_ID = "pack_cobranza"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants cuyo AgentSpec incluye este pack (ADR-0019)."""
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()

    result: list[uuid.UUID] = []
    for raw in rows:
        tid = uuid.UUID(str(raw))
        try:
            async with tenant_scope(tid):
                spec = await load_active_spec()
            if PACK_ID in enabled_packs(spec):
                result.append(tid)
        except Exception as exc:
            log.warning(
                "cobranza_tenant_scope_failed", tenant_id=str(tid), error=type(exc).__name__
            )
            continue
    return result


@register_cron("pack_cobranza", "0 2 * * *")
async def marcar_mora() -> None:
    """Marca cuotas vencidas como `atrasada` y recalcula el color de riesgo del cliente."""
    from datetime import date

    hoy = date.today()
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                await session.execute(
                    update(Cuota)
                    .where(
                        Cuota.tenant_id == tenant_id,
                        Cuota.estatus == "pendiente",
                        Cuota.fecha_vencimiento < hoy,
                    )
                    .values(estatus="atrasada")
                )
                # Recalcula color de riesgo: rojo si tiene cuotas atrasadas.
                clientes = (
                    await session.execute(
                        select(Cliente).where(
                            Cliente.tenant_id == tenant_id, Cliente.deleted_at.is_(None)
                        )
                    )
                ).scalars().all()
                for cliente in clientes:
                    prestamos = (
                        await session.execute(
                            select(Prestamo.id).where(
                                Prestamo.tenant_id == tenant_id,
                                Prestamo.cliente_id == cliente.id,
                                Prestamo.estatus == "vigente",
                            )
                        )
                    ).scalars().all()
                    if not prestamos:
                        nuevo = "blanco"
                    else:
                        atrasadas = (
                            await session.execute(
                                select(Cuota.id).where(
                                    Cuota.tenant_id == tenant_id,
                                    Cuota.prestamo_id.in_(prestamos),
                                    Cuota.estatus == "atrasada",
                                )
                            )
                        ).scalars().all()
                        nuevo = "rojo" if atrasadas else "azul"
                    if cliente.color_riesgo != nuevo:
                        cliente.color_riesgo = nuevo
            log.info("cobranza_mora_actualizada", tenant_id=str(tenant_id))
