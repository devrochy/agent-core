"""Configuración avanzada del agente (pestaña Avanzado).

Fuente única del panel SSR y la API: proveedor LLM (BYOK), flow de atención (lectura, ADR-0054) y últimas
corridas de evals. Las versiones del AgentSpec y los guardrails personalizados se retiraron con el v2
(ADR-0206, HU-AOS-91).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from core.db.models import AppSettings
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

# ------------------------------------------------------------------ LLM (BYOK)
async def _app_settings(tenant_id: uuid.UUID | None) -> AppSettings | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def read_llm(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    from core.config import get_settings

    settings = get_settings()
    app_settings = await _app_settings(tenant_id)
    return {
        "llm_provider": (app_settings.llm_provider if app_settings else None) or settings.llm_provider,
        "llm_model": (app_settings.llm_model if app_settings else None) or settings.llm_model,
        "has_api_key": bool(app_settings and app_settings.llm_api_key_encrypted),
    }


async def save_llm(
    tenant_id: uuid.UUID | None,
    *,
    llm_provider: str,
    llm_model: str,
    llm_api_key: str = "",
) -> None:
    from core.db.models import utcnow  # noqa: F401  (import explícito por claridad)
    from core.shared.crypto import get_secret_box

    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=tenant_id)
            session.add(app_settings)
        app_settings.llm_provider = llm_provider or None
        app_settings.llm_model = llm_model or None
        if llm_api_key:
            app_settings.llm_api_key_encrypted = get_secret_box().encrypt(llm_api_key)


# ------------------------------------------------------------------ flow y evals (lectura)
@dataclass(frozen=True)
class _PacksDelNegocio:
    """Lo único que `resolve_flow` lee del spec: los packs cuyos flujos compone (los del negocio, ADR-0206 §5)."""

    packs: tuple[str, ...]


async def read_flow() -> dict[str, Any]:
    """El flujo del negocio con las habilidades del negocio (`packs_del_negocio`). Sin declarativas: eran del
    AgentSpec v2 (HU-AOS-91)."""
    from core.agents.flow import resolve_flow
    from core.agents.instalaciones_agente import CORE_PACK_ID, packs_del_negocio
    from core.mcp.host import mcp_host

    enabled = await packs_del_negocio()
    available = {t.name for t in mcp_host.get_tools() if t.plugin_id in enabled}
    resolved = await resolve_flow(_PacksDelNegocio(tuple(sorted(enabled - {CORE_PACK_ID}))), available=available)
    return {
        "source": resolved.source if resolved else "",
        "sections": [
            {"label": s.label, "purpose": s.purpose, "tools": s.tools}
            for s in (resolved.sections if resolved else [])
        ],
        "unknown_tools": resolved.unknown_tools if resolved else [],
    }


async def recent_eval_runs(tenant_id: uuid.UUID | None, limit: int = 10) -> list[dict[str, Any]]:
    if tenant_id is None:
        return []
    from core.db.models import EvalRun

    try:
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(EvalRun)
                        .where(EvalRun.tenant_id == tenant_id)
                        .order_by(EvalRun.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        return []
    return [
        {
            "id": str(r.id),
            "status": getattr(r, "status", ""),
            "score": getattr(r, "score", None),
            "created_at": getattr(r, "created_at", None),
        }
        for r in rows
    ]
