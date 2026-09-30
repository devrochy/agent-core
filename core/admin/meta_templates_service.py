"""Plantillas de WhatsApp de una conexión Meta (fuente única SSR + API).

- `list_templates`: sincroniza (con lock de 1 h) y lista las plantillas del WABA.
- `sync_templates_now`: sync manual (mismo lock que el listado).
- `delete_template`: borra TODAS las versiones de idioma.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.admin.meta_numbers_service import resolve_waba_and_token
from core.db.models import MessageTemplate
from core.db.session import get_owned, get_session


async def list_templates(
    tenant_id: uuid.UUID, connection_id: str
) -> tuple[list[dict], str | None, bool]:
    """(plantillas, error de sync, en_límite)."""
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return [], "Conexión inválida", False

    connection, waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None:
        return [], "Conexión no encontrada", False

    sync_error = None
    if waba is not None and token and await acquire_sync_lock(waba.waba_id):
        try:
            await sync_templates(
                waba.waba_id,
                token,
                get_settings().meta_app_secret,
                connection.graph_api_version,
                connection.id,
                tenant_id,
            )
        except GraphError as exc:
            sync_error = exc.message

    rows: list[MessageTemplate] = []
    if waba is not None:
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(MessageTemplate)
                        .where(
                            MessageTemplate.waba_account_id == waba.id,
                            MessageTemplate.tenant_id == tenant_id,
                        )
                        .order_by(MessageTemplate.name, MessageTemplate.language)
                    )
                )
                .scalars()
                .all()
            )

    at_limit = bool(
        waba is not None
        and waba.message_template_limit is not None
        and (waba.message_template_count or 0) >= waba.message_template_limit
    )
    items = [
        {
            "id": t.id,
            "name": t.name,
            "language": t.language,
            "category": t.category,
            "status": t.status,
            "quality_score": t.quality_score,
            "rejected_reason": t.rejected_reason,
        }
        for t in rows
    ]
    return items, sync_error, at_limit


async def sync_templates_now(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None or waba is None or not token:
        return "No hay conexión/WABA lista todavía"
    if not await acquire_sync_lock(waba.waba_id):
        return "Ya se sincronizó en la última hora"

    try:
        await sync_templates(
            waba.waba_id,
            token,
            get_settings().meta_app_secret,
            connection.graph_api_version,
            connection.id,
            tenant_id,
        )
    except GraphError as exc:
        return exc.message[:200]
    return None


async def delete_template(
    tenant_id: uuid.UUID, connection_id: str, template_id: str
) -> str | None:
    """Borra todas las versiones de idioma de una plantilla."""
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import delete_template_all_languages

    try:
        cid = uuid.UUID(connection_id)
        tid_template = uuid.UUID(template_id)
    except ValueError:
        return "Plantilla inválida"

    connection, waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None or waba is None or not token:
        return "No hay conexión/WABA lista todavía"

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, tid_template, tenant_id)
    if row is None:
        return "Plantilla no encontrada"

    try:
        await delete_template_all_languages(
            waba.waba_id,
            row.name,
            token,
            get_settings().meta_app_secret,
            connection.graph_api_version,
            connection.id,
            tenant_id,
        )
    except GraphError as exc:
        return exc.message[:200]
    return None
