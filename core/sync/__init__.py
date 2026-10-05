"""Sync incremental de la app oficial (ADR-0097, HU-APP-15).

Mismo patrón que la búsqueda global (ADR-0096): el core expone
`GET /api/v1/sync/{entidad}` y ejecuta **adapters** que registran los packs (el
core no importa de los packs, invariante 5). Cada adapter declara:

- `entidad`: clave pública de la URL (`clientes`, `carteras`, `citas`, …).
- `seccion`: sección RBAC que protege la entidad.
- `model`: modelo SQLAlchemy (tabla de negocio con RLS).
- `marcador_sql`: columna (o expresión) monotónica para el cursor.
- `marcador_row`: valor del marcador en una fila cargada.
- `tiene_deleted_at`: si la tabla soft-deletea (para `deletedIds`).
- `serialize(row) -> dict`: DTO móvil de la entidad.

Contrato de respuesta: `{entidad, items, nextCursor, deletedIds}`. El cursor es
ISO-8601; `nextCursor` es el marcador máximo devuelto (o el cursor recibido si
no hubo filas). Un registro borrado y re-creado en la misma ventana puede
aparecer en `items` y `deletedIds`: la app aplica borrado al final.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SyncAdapter:
    entidad: str
    seccion: str
    label: str
    model: Any
    # `Any`: acepta `InstrumentedAttribute` y expresiones (`coalesce(...)`); el
    # tipado estricto de ColumnElement no acomoda ambas (deuda menor).
    marcador_sql: Any
    marcador_row: Callable[[Any], datetime]
    serialize: Callable[[Any], dict[str, Any]]
    tiene_deleted_at: bool


_REGISTRY: dict[str, SyncAdapter] = {}


def register_sync(adapter: SyncAdapter) -> None:
    if adapter.entidad in _REGISTRY:
        return
    _REGISTRY[adapter.entidad] = adapter


def sync_adapters() -> dict[str, SyncAdapter]:
    return dict(_REGISTRY)


def parse_cursor(cursor: str | None) -> datetime:
    if not cursor:
        return EPOCH
    try:
        value = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("cursor inválido") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


async def ejecutar_sync(
    adapter: SyncAdapter, tenant_id: uuid.UUID, cursor_dt: datetime, limit: int
) -> tuple[list[dict[str, Any]], datetime, list[str]]:
    model = adapter.model
    async with get_session() as session:
        rows = (
            await session.execute(
                select(model)
                .where(
                    model.tenant_id == tenant_id,
                    adapter.marcador_sql > cursor_dt,
                )
                .order_by(adapter.marcador_sql.asc(), model.id.asc())
                .limit(limit)
            )
        ).scalars().all()

        items = [adapter.serialize(r) for r in rows]
        next_dt = cursor_dt
        for r in rows:
            valor = adapter.marcador_row(r)
            if valor is not None and valor > next_dt:
                next_dt = valor

        deleted_ids: list[str] = []
        if adapter.tiene_deleted_at:
            deleted_col = model.deleted_at
            borrados = (
                await session.execute(
                    select(model.id, deleted_col)
                    .where(
                        model.tenant_id == tenant_id,
                        deleted_col > cursor_dt,
                    )
                    .order_by(deleted_col.asc(), model.id.asc())
                    .limit(limit)
                )
            ).all()
            deleted_ids = [str(x) for x, _ in borrados]
            # Los borrados también avanzan el cursor: una ventana con solo
            # borrados no debe devolver el mismo cursor (loop infinito).
            for _, borrado_en in borrados:
                if borrado_en is not None and borrado_en > next_dt:
                    next_dt = borrado_en

    return items, next_dt, deleted_ids
