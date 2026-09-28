"""Configuración avanzada del agente (pestaña Avanzado).

Fuente única del panel SSR y la API: versiones del `AgentSpec` (activación/rollback,
ADR-0051), proveedor LLM (BYOK), guardrails personalizados (DSL seguro, ADR-0051),
flow de atención (lectura, ADR-0054) y últimas corridas de evals.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.agents.spec import activate_spec_version, load_active_spec, save_spec_if_changed
from core.db.models import AgentSpecRecord, AppSettings
from core.db.session import get_session


# ------------------------------------------------------------------ versiones
async def list_spec_versions(tenant_id: uuid.UUID | None) -> list[dict[str, Any]]:
    if tenant_id is None:
        return []
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(AgentSpecRecord)
                    .where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                    .order_by(AgentSpecRecord.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(r.id),
            "version": r.version,
            "is_active": r.is_active,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def activate_version(
    tenant_id: uuid.UUID | None, record_id: str
) -> tuple[str | None, str | None]:
    """(versión_activada, error)."""
    if tenant_id is None:
        return None, "No hay negocio activo"
    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        return None, "Versión inválida"
    try:
        version = await activate_spec_version(tenant_id, record_id=rid)
    except ValueError as exc:
        return None, str(exc)[:140]
    return version, None


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


# ------------------------------------------------------------------ guardrails propios
def parse_custom_guardrails(text: str) -> list[dict[str, Any]]:
    """Parsea guardrails propios, una regla por línea (DSL seguro)."""
    rules: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        kind = parts[0]
        if kind == "max_length":
            try:
                value = int(parts[1])
            except ValueError:
                continue
            action = parts[2] if len(parts) > 2 else "retry"
            rules.append({"kind": kind, "value": value, "action": action})
        elif kind == "required_tool":
            if len(parts) < 3:
                continue
            phrases = [p.strip() for p in parts[1].split(",") if p.strip()]
            tool = parts[2]
            action = parts[3] if len(parts) > 3 else "retry"
            rules.append(
                {"kind": kind, "phrases": phrases, "tool": tool, "action": action}
            )
        elif kind == "forbidden_phrase":
            phrases = [p.strip() for p in parts[1].split(",") if p.strip()]
            action = parts[2] if len(parts) > 2 else "retry"
            rules.append({"kind": kind, "phrases": phrases, "action": action})
    return rules


def format_custom_guardrails(rules: list[dict[str, Any]]) -> str:
    """Reglas → texto editable (una por línea)."""
    lines: list[str] = []
    for rule in rules or []:
        kind = rule.get("kind")
        action = rule.get("action", "retry")
        if kind == "max_length":
            lines.append(f"max_length | {rule.get('value')} | {action}")
        elif kind == "required_tool":
            phrases = ", ".join(rule.get("phrases") or [])
            lines.append(f"required_tool | {phrases} | {rule.get('tool')} | {action}")
        elif kind == "forbidden_phrase":
            phrases = ", ".join(rule.get("phrases") or [])
            lines.append(f"forbidden_phrase | {phrases} | {action}")
    return "\n".join(lines)


def read_custom_guardrails(spec) -> str:  # type: ignore[no-untyped-def]
    return format_custom_guardrails((spec.extensions or {}).get("guardrails") or [])


async def save_custom_guardrails(
    tenant_id: uuid.UUID | None, text: str
) -> str | None:
    from core.orchestrator.guardrails import validate_custom_rules

    spec = await load_active_spec()
    data = spec.model_dump(mode="json")
    extensions = dict(data.get("extensions") or {})
    try:
        extensions["guardrails"] = validate_custom_rules(parse_custom_guardrails(text))
    except Exception as exc:
        return str(exc)[:160]
    data["extensions"] = extensions
    await save_spec_if_changed(spec, data, tenant_id)
    return None


# ------------------------------------------------------------------ flow y evals (lectura)
async def read_flow(spec) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    from core.agents.flow import resolve_flow
    from core.mcp.declarative import tools_for_spec
    from core.mcp.host import mcp_host

    enabled = set(spec.packs or []) | {"core"}
    available = {
        t.name for t in mcp_host.get_tools() if t.plugin_id in enabled
    } | {t.name for t in tools_for_spec(spec)}
    resolved = await resolve_flow(spec, available=available)
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
