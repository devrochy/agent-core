"""Sync paginado de plantillas de WhatsApp -- mismo patrón que
`meta_phone_numbers.py::list_phone_numbers`/`sync_phone_numbers`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from core.db.models import MessageTemplate, WabaAccount, utcnow
from core.db.session import get_session
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_templates import get_template_limits
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

_TEMPLATE_FIELDS = "id,name,status,category,language,components,quality_score,parameter_format"


async def list_templates(waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID) -> list[dict[str, Any]]:
    """Pagina siguiendo `paging.cursors.after` hasta agotar páginas."""
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        query = {"fields": _TEMPLATE_FIELDS, "limit": "50"}
        if after:
            query["after"] = after
        data = await graph_fetch(
            f"/{waba_id}/message_templates", token=token, app_secret=app_secret,
            query=query, version=version, connection_id=connection_id,
        )
        items.extend((data or {}).get("data", []))
        after = ((data or {}).get("paging", {}).get("cursors", {}) or {}).get("after")
        next_url = (data or {}).get("paging", {}).get("next")
        if not after or not next_url:
            break
    return items


async def acquire_sync_lock(tenant_id: uuid.UUID, waba_id: str) -> bool:
    """Traduce el `jobId` de BullMQ (`tpl:{wabaId}:{yyyy-MM-dd-HH}`) pedido
    en el ticket a un lock de Redis: cron y botón manual comparten esta
    llave, así una sync manual no repite lo que el cron ya hizo esa hora.

    La llave lleva el tenant: `waba_id` no es único entre negocios, y sin él un
    negocio podía gastar el turno horario de otro (auditor-multitenant, HU-CONN-01)."""
    redis = get_redis()
    hour_key = datetime.now(UTC).strftime("%Y-%m-%d-%H")
    key = f"tpl_sync_lock:{tenant_id}:{waba_id}:{hour_key}"
    return bool(await redis.set(key, "1", ex=3600, nx=True))


def _quality_score(item: dict[str, Any]) -> str | None:
    """`quality_score` de Graph: desde v2x llega como objeto (`{"score": "GREEN",
    "date": ...}`); antes era texto. La columna es texto: se guarda el `score`.
    Con el objeto crudo el INSERT fallaba y ninguna plantilla se sincronizaba."""
    valor = item.get("quality_score")
    if isinstance(valor, dict):
        valor = valor.get("score")
    return str(valor) if valor else None


async def sync_templates(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Upsert por `(waba_account_id, name, language)` -- idempotente por la
    unique constraint ya existente en el esquema. Además refresca los
    límites cacheados en `WabaAccount`."""
    items = await list_templates(waba_id, token, app_secret, version, connection_id)

    async with get_session() as session:
        waba_row = (
            await session.execute(
                select(WabaAccount).where(WabaAccount.waba_id == waba_id, WabaAccount.connection_id == connection_id)
            )
        ).scalar_one_or_none()
        waba_account_id = waba_row.id if waba_row else None

    for item in items:
        name, language = item.get("name"), item.get("language")
        if not name or not language or waba_account_id is None:
            continue
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(MessageTemplate).where(
                        MessageTemplate.waba_account_id == waba_account_id,
                        MessageTemplate.name == name,
                        MessageTemplate.language == language,
                        MessageTemplate.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    MessageTemplate(
                        tenant_id=tenant_id, waba_account_id=waba_account_id,
                        meta_template_id=str(item.get("id") or "") or None,
                        name=name, language=language, category=item.get("category"),
                        parameter_format=item.get("parameter_format"), status=item.get("status") or "PENDING",
                        quality_score=_quality_score(item), components=item.get("components"),
                        synced_at=utcnow(),
                    )
                )
            else:
                existing.meta_template_id = str(item.get("id") or "") or existing.meta_template_id
                existing.category = item.get("category")
                existing.parameter_format = item.get("parameter_format")
                existing.status = item.get("status") or existing.status
                existing.quality_score = _quality_score(item)
                existing.components = item.get("components")
                existing.synced_at = utcnow()
                existing.sync_error = None
                existing.updated_at = utcnow()

    if waba_row is not None:
        try:
            summary = await get_template_limits(waba_id, token, app_secret, version, connection_id)
        except Exception:
            log.exception("template_limits_sync_fallo", waba_id=waba_id)
        else:
            async with get_session() as session:
                row = await session.get(WabaAccount, waba_row.id)
                if row is not None:
                    row.message_template_count = summary.get("message_template_count")
                    row.message_template_limit = summary.get("message_template_limit")
                    row.are_translations_complete = summary.get("are_translations_complete")
                    row.template_limits_synced_at = utcnow()

    return items
