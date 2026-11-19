"""Providers de búsqueda del pack de Cobranza (ADR-0096).

Registra `pack_cobranza:clientes` (nombre, documento o teléfono) y
`pack_cobranza:promesas` (por nombre del cliente). Corre dentro del scope de
tenant del request; RLS aplica sobre `pack_cobranza_clientes` y
`pack_cobranza_promesas`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from core.db.session import get_session
from core.search import ResultadoBusqueda, escapar_ilike, register_search
from pack_cobranza.models import Cliente, Promesa


async def _buscar_clientes(
    tenant_id: uuid.UUID, q: str, limit: int
) -> list[ResultadoBusqueda]:
    patron = f"%{escapar_ilike(q)}%"
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Cliente)
                .where(
                    Cliente.tenant_id == tenant_id,
                    Cliente.deleted_at.is_(None),
                    or_(
                        Cliente.nombre.ilike(patron, escape="\\"),
                        Cliente.apellido.ilike(patron, escape="\\"),
                        Cliente.numero_documento.ilike(patron, escape="\\"),
                        Cliente.telefono_whatsapp.ilike(patron, escape="\\"),
                    ),
                )
                .order_by(Cliente.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": str(c.id),
            "tipo": "cliente_cobranza",
            "titulo": f"{c.nombre} {c.apellido or ''}".strip(),
            "subtitulo": c.numero_documento or c.telefono_whatsapp or "",
            "extra": {"carteraId": str(c.cartera_id), "colorRiesgo": c.color_riesgo},
        }
        for c in rows
    ]


async def _buscar_promesas(
    tenant_id: uuid.UUID, q: str, limit: int
) -> list[ResultadoBusqueda]:
    patron = f"%{escapar_ilike(q)}%"
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Promesa, Cliente)
                .join(Cliente, Cliente.id == Promesa.cliente_id)
                .where(
                    Promesa.tenant_id == tenant_id,
                    Promesa.deleted_at.is_(None),
                    or_(
                        Cliente.nombre.ilike(patron, escape="\\"),
                        Cliente.apellido.ilike(patron, escape="\\"),
                    ),
                )
                .order_by(Promesa.fecha_prometida.desc())
                .limit(limit)
            )
        ).all()
    return [
        {
            "id": str(p.id),
            "tipo": "promesa",
            "titulo": f"{c.nombre} {c.apellido or ''}".strip(),
            "subtitulo": f"Promesa {p.fecha_prometida.isoformat()} · {p.estado}",
            "extra": {"clienteId": str(c.id), "estado": p.estado},
        }
        for p, c in rows
    ]


register_search("pack_cobranza:clientes", "Clientes de cobranza", _buscar_clientes)
register_search("pack_cobranza:promesas", "Promesas", _buscar_promesas)
