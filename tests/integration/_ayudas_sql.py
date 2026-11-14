"""Ayudas compartidas de los tests de integración: módulos del binario, SQL en el scope de un negocio
y una conexión de canal a mano."""

from __future__ import annotations

import uuid
from typing import Any

from _api_helpers import uniq
from sqlalchemy import text


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _conexion(tid: uuid.UUID, tipo: str | None, *, channel: str = "whatsapp", provider: str = "meta") -> uuid.UUID:
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, :ch, :pr, :ext, :tipo)",
        id=cid, t=tid, ch=channel, pr=provider, ext=uniq("ext"), tipo=tipo,
    )
    return cid
