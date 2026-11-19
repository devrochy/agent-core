"""Topes por plan del conocimiento (ADR-0145 §2): el pack cuenta lo suyo.

`documentos` son los documentos vivos del negocio (sin `deleted_at`) y `conocimiento_mb`, la
suma de sus `bytes` en MB. El core solo aporta los límites del plan (`core/billing/plans.py`) y
el advisory lock con el 409 `plan_limit_<clave>` (`enforcement.enforce_pack_growth`).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from core.billing.enforcement import enforce_pack_growth
from core.shared.tenant import get_current_tenant_id
from pack_knowledge.models import KnowledgeDocument

MB = 1024 * 1024


async def contar_documentos(session: Any, tenant_id: uuid.UUID) -> int:
    consulta = select(func.count(KnowledgeDocument.id)).where(
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.deleted_at.is_(None)
    )
    return int((await session.execute(consulta)).scalar() or 0)


async def contar_mb(session: Any, tenant_id: uuid.UUID) -> float:
    consulta = select(func.coalesce(func.sum(KnowledgeDocument.bytes), 0)).where(
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.deleted_at.is_(None)
    )
    return int((await session.execute(consulta)).scalar() or 0) / MB


async def exigir_cupo(session: Any, *, bytes_nuevos: int) -> None:
    """Levanta `QuotaExceeded` si un documento nuevo de `bytes_nuevos` pasa un tope del plan.

    Corre en la sesión que va a insertar el documento, dentro del `tenant_scope` del negocio, y
    toma el tenant de ese scope (nunca de un argumento). Los locks duran hasta su commit, así dos
    subidas concurrentes no pasan las dos con un cupo.
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("exigir_cupo requiere un tenant_scope")

    async def _documentos(s: Any) -> float:
        return await contar_documentos(s, tenant_id)

    async def _mb(s: Any) -> float:
        return await contar_mb(s, tenant_id)

    await enforce_pack_growth(tenant_id, "documentos", _documentos, session=session)
    await enforce_pack_growth(tenant_id, "conocimiento_mb", _mb, incremento=bytes_nuevos / MB, session=session)
