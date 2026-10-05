"""Providers de búsqueda del pack de Comercio (ADR-0096).

Una sola sección RBAC (`pack_commerce`, el plugin_id: su menú no declara
sub-secciones) con dos tipos: productos (por nombre) y pedidos (por número
público `OC-NNNN` o nombre de entrega). RLS aplica sobre las tablas del pack.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from core.db.session import get_session
from core.search import ResultadoBusqueda, escapar_ilike, register_search
from pack_commerce.models import Order, Product


async def _buscar_comercio(
    tenant_id: uuid.UUID, q: str, limit: int
) -> list[ResultadoBusqueda]:
    patron = f"%{escapar_ilike(q)}%"
    resultados: list[ResultadoBusqueda] = []
    async with get_session() as session:
        productos = (
            await session.execute(
                select(Product)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.name.ilike(patron, escape="\\"),
                )
                .order_by(Product.name)
                .limit(limit)
            )
        ).scalars().all()
        pedidos = (
            await session.execute(
                select(Order)
                .where(
                    Order.tenant_id == tenant_id,
                    or_(
                        Order.public_order_number.ilike(patron, escape="\\"),
                        Order.delivery_full_name.ilike(patron, escape="\\"),
                    ),
                )
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    resultados.extend(
        {
            "id": str(p.id),
            "tipo": "producto",
            "titulo": p.name,
            "subtitulo": "Producto",
            "extra": {"variantes": p.has_variants},
        }
        for p in productos
    )
    resultados.extend(
        {
            "id": str(o.id),
            "tipo": "pedido",
            "titulo": o.public_order_number,
            "subtitulo": f"Pedido · {o.status}",
            "extra": {"estado": o.status, "total": str(o.total)},
        }
        for o in pedidos
    )
    return resultados[:limit]


register_search("pack_commerce", "Comercio", _buscar_comercio)
