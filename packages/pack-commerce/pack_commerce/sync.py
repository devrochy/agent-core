"""Adapters de sync del pack de Comercio (ADR-0097).

Entidades: `productos`, `categorias`, `promociones` y `pedidos` (sección
`pack_commerce`). Productos y promociones no tienen soft delete (commerce v1);
pedidos usa `COALESCE(status_changed_at, created_at)` como marcador de cambio.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func

from core.sync import SyncAdapter, register_sync
from pack_commerce.fotos import url_foto
from pack_commerce.models import Category, Order, Product, Promotion


def _serializar_producto(row: Product) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "nombre": row.name,
        "descripcion": row.description,
        "imagenUrl": url_foto(row),
        "categoriaId": str(row.category_id) if row.category_id else None,
        "variantes": row.has_variants,
        "updatedAt": row.updated_at.isoformat(),
    }


def _serializar_categoria(row: Category) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "nombre": row.name,
        "orden": row.sort_order,
        "activa": row.active,
        "updatedAt": row.updated_at.isoformat(),
    }


def _serializar_promocion(row: Promotion) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tipo": row.type,
        "rules": row.rules,
        "activa": row.active,
        "vigenciaDesde": row.valid_from.isoformat() if row.valid_from else None,
        "vigenciaHasta": row.valid_to.isoformat() if row.valid_to else None,
        "updatedAt": row.updated_at.isoformat(),
    }


def _marcador_pedido(row: Order) -> datetime:
    return row.status_changed_at or row.created_at


def _serializar_pedido(row: Order) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "numero": row.public_order_number,
        "estado": row.status,
        "estadoPago": row.payment_status,
        "total": str(row.total),
        "entrega": row.delivery_full_name,
        "updatedAt": _marcador_pedido(row).isoformat(),
    }


register_sync(
    SyncAdapter(
        entidad="productos",
        seccion="pack_commerce",
        label="Productos",
        model=Product,
        marcador_sql=Product.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_producto,
        tiene_deleted_at=False,
    )
)

register_sync(
    SyncAdapter(
        entidad="categorias",
        seccion="pack_commerce",
        label="Categorías",
        model=Category,
        marcador_sql=Category.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_categoria,
        tiene_deleted_at=True,
    )
)

register_sync(
    SyncAdapter(
        entidad="promociones",
        seccion="pack_commerce",
        label="Promociones",
        model=Promotion,
        marcador_sql=Promotion.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_promocion,
        tiene_deleted_at=False,
    )
)

register_sync(
    SyncAdapter(
        entidad="pedidos",
        seccion="pack_commerce",
        label="Pedidos",
        model=Order,
        marcador_sql=func.coalesce(Order.status_changed_at, Order.created_at),
        marcador_row=_marcador_pedido,
        serialize=_serializar_pedido,
        tiene_deleted_at=False,
    )
)
