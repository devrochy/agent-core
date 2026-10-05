"""Adapters de sync del pack de Cobranza (ADR-0097).

Entidades: `clientes_cobranza` (`pack_cobranza:clientes`), `carteras`
(`pack_cobranza:carteras`) y `promesas` (`pack_cobranza:promesas`), todas con
`updated_at` y soft delete. RLS aplica dentro del scope del request.
"""

from __future__ import annotations

from typing import Any

from core.sync import SyncAdapter, register_sync
from pack_cobranza.models import Cartera, Cliente, Promesa


def _serializar_cliente(row: Cliente) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "carteraId": str(row.cartera_id),
        "nombre": f"{row.nombre} {row.apellido or ''}".strip(),
        "negocio": row.negocio,
        "telefonoWhatsapp": row.telefono_whatsapp,
        "numeroDocumento": row.numero_documento,
        "colorRiesgo": row.color_riesgo,
        "estatus": row.estatus,
        "updatedAt": row.updated_at.isoformat(),
    }


def _serializar_cartera(row: Cartera) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "gestorId": str(row.gestor_id) if row.gestor_id else None,
        "nombre": row.nombre,
        "descripcion": row.descripcion,
        "moneda": row.moneda,
        "numCuotas": row.num_cuotas,
        "estatus": row.estatus,
        "updatedAt": row.updated_at.isoformat(),
    }


def _serializar_promesa(row: Promesa) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "prestamoId": str(row.prestamo_id),
        "clienteId": str(row.cliente_id),
        "fechaPrometida": row.fecha_prometida.isoformat(),
        "valorPrometido": str(row.valor_prometido),
        "estado": row.estado,
        "tipo": row.tipo,
        "updatedAt": row.updated_at.isoformat(),
    }


register_sync(
    SyncAdapter(
        entidad="clientes_cobranza",
        seccion="pack_cobranza:clientes",
        label="Clientes de cobranza",
        model=Cliente,
        marcador_sql=Cliente.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_cliente,
        tiene_deleted_at=True,
    )
)

register_sync(
    SyncAdapter(
        entidad="carteras",
        seccion="pack_cobranza:carteras",
        label="Carteras",
        model=Cartera,
        marcador_sql=Cartera.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_cartera,
        tiene_deleted_at=True,
    )
)

register_sync(
    SyncAdapter(
        entidad="promesas",
        seccion="pack_cobranza:promesas",
        label="Promesas",
        model=Promesa,
        marcador_sql=Promesa.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_promesa,
        tiene_deleted_at=True,
    )
)
