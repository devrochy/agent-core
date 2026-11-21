"""Ocultar el comercio de un negocio que lo apagó (HU-AOS-71, ❓7).

Marca con `deleted_at` los productos, pedidos y cotizaciones vivos del negocio del scope: dejan de verse en
el panel, la APK, el agente y los crons, siguen en la base (invariante 8) y no reaparecen si comercio se
vuelve a encender (nada los des-oculta). Idempotente: lo ya oculto no se toca, así que repetirlo da cero.

Las guardas del orden seguro (lead del formulario en el intake nuevo, comercio apagado) las aplica el CLI
`agent-core tenant ocultar-comercio`; aquí solo se oculta.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update

from core.db.models import utcnow
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import Order, Product, Quote

_MODELOS = (("productos", Product), ("pedidos", Order), ("cotizaciones", Quote))


@dataclass(frozen=True)
class Ocultos:
    productos: int
    pedidos: int
    cotizaciones: int

    @property
    def total(self) -> int:
        return self.productos + self.pedidos + self.cotizaciones


def _scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("ocultar_comercio: el scope no es el del negocio")


async def contar_vivos(tenant_id: uuid.UUID) -> Ocultos:
    """Lo que se ocultaría (dry-run)."""
    _scope(tenant_id)
    async with get_session() as session:
        cuentas = {
            nombre: (
                await session.execute(
                    select(func.count())
                    .select_from(modelo)
                    .where(modelo.tenant_id == tenant_id, modelo.deleted_at.is_(None))
                )
            ).scalar_one()
            for nombre, modelo in _MODELOS
        }
    return Ocultos(**cuentas)


async def ocultar_comercio(tenant_id: uuid.UUID) -> Ocultos:
    """Oculta en una transacción los productos, pedidos y cotizaciones vivos del negocio. Devuelve cuántos."""
    _scope(tenant_id)
    ahora = utcnow()
    cuentas: dict[str, int] = {}
    async with get_session() as session:
        for nombre, modelo in _MODELOS:
            resultado = await session.execute(
                update(modelo)
                .where(modelo.tenant_id == tenant_id, modelo.deleted_at.is_(None))
                .values(deleted_at=ahora)
            )
            cuentas[nombre] = resultado.rowcount or 0
    return Ocultos(**cuentas)
