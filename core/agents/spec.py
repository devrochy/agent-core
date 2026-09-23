"""AgentSpec desde base de datos (Sprint 2, ADR-0014).

El AgentSpec es la configuración declarativa del agente, validada contra el
contrato congelado. Vive como fila por tenant (tabla `agent_specs`). La carga
siempre ocurre dentro de un `tenant_scope`: RLS acota la fila al tenant actual.
"""

from __future__ import annotations

import uuid
from typing import Any

from agentcore_contracts import AgentSpec
from sqlalchemy import select

from core.db.models import AgentSpecRecord
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

CORE_PACK_ID = "core"


def enabled_packs(spec: AgentSpec) -> set[str]:
    """Packs habilitados para el tenant según `spec.packs` (ADR-0019).

    El pack del core ("core") siempre está habilitado: sus tools son plomería del
    framework y no se filtran por pack del tenant.
    """
    return set(spec.packs) | {CORE_PACK_ID}

DEFAULT_VERSION = "1.0.0"

# Spec default SIN texto de prompts (ADR-0014): identidad/tono/reglas viven en los
# playbooks referenciados por `playbooks`, no aquí.
# v2.0.0 (ADR-0050): sin `locale`, sin `summarize`, sin `policies.escalation`/
# `confirmBeforeWrite`, sin `rag.collections`; con `extensions`.
DEFAULT_SPEC: dict[str, Any] = {
    "id": "agent-default",
    "version": DEFAULT_VERSION,
    "tenantId": "",
    "persona": {
        "role": "asistente",
        "tone": "cercano",
        "warmth": 3,
        "emojis": "sparse",
        "maxLength": 500,
        "forbiddenPhrases": [],
    },
    "model": {"primary": "deepseek-chat", "temperature": 0.3},
    "memory": {"shortTermTurns": 30},
    "playbooks": [
        {"name": "identidad", "description": "Identidad del asistente"},
        {"name": "tono", "description": "Voz y tono del asistente"},
        {"name": "reglas-operacion", "description": "Reglas de operación del asistente"},
        {"name": "respuestas", "description": "Mensajes de cierre y avisos del asistente"},
    ],
    "tools": [],
    "policies": {
        # La escalación operativa vive en `AppSettings.escalation_config["keywords"]`
        # (core/orchestrator/consumer.py); el spec ya no la declara (ADR-0050).
        "maxToolCallsPerTurn": 10,
        "budgetUsdPerConversation": 0.5,
    },
    "packs": [],
    "extensions": {},
}


# Campos eliminados en v2.0.0 (ADR-0050) que pueden seguir presentes en specs v1
# guardados en BD: se descartan al cargar para no romper la validación. Mapa:
# ruta del contenedor → claves a quitar (None = quitar el contenedor entero).
_LEGACY_SPEC_FIELDS: dict[str, tuple[str, ...] | None] = {
    "persona": ("locale",),
    "memory": ("summarize",),
    "policies": ("escalation", "confirmBeforeWrite"),
}
_LEGACY_RAG_FIELDS: tuple[str, ...] = ("collections",)


def _drop_legacy_fields(spec_data: dict[str, Any]) -> dict[str, Any]:
    """Quita campos de v1 que ya no existen en v2 (tolerante, sin migración dura)."""
    data = dict(spec_data)
    for container, keys in _LEGACY_SPEC_FIELDS.items():
        block = data.get(container)
        if not isinstance(block, dict):
            continue
        block = dict(block)
        for key in keys:
            block.pop(key, None)
        data[container] = block
    rag = data.get("memory", {})
    if isinstance(rag, dict) and isinstance(rag.get("rag"), dict):
        memory = dict(rag)
        rag_block = dict(memory["rag"])
        for key in _LEGACY_RAG_FIELDS:
            rag_block.pop(key, None)
        memory["rag"] = rag_block
        data["memory"] = memory
    return data


DEFAULT_PLAYBOOK_REFS: list[dict[str, str]] = list(DEFAULT_SPEC["playbooks"])


async def _migrate_legacy_escalation(tenant_id: uuid.UUID, raw: dict[str, Any]) -> None:
    """Rescata `policies.escalation.triggers` de un spec v1 a `AppSettings` (ADR-0050).

    El contrato v2 eliminó `policies.escalation`; antes de que `_drop_legacy_fields`
    lo descarte, si el tenant no tiene keywords propias de escalamiento, se copian
    desde el spec (evita perder una configuración real de forma silenciosa).
    """
    triggers = (
        (raw.get("policies") or {}).get("escalation") or {}
    ).get("triggers")
    if not triggers:
        return
    from core.db.models import AppSettings

    async with get_session() as session:
        settings_row = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if settings_row is None:
            return
        config = dict(settings_row.escalation_config or {})
        if config.get("keywords"):
            return  # ya tiene keywords propias: no se pisa
        config["keywords"] = list(triggers)
        settings_row.escalation_config = config


async def _ensure_default_playbooks(tenant_id: uuid.UUID, spec: AgentSpec) -> AgentSpec:
    """Fusiona los playbooks default en el índice si faltan (data migration perezosa).

    Los specs sembrados antes de añadir `respuestas` no la tienen; el merge es
    idempotente y solo escribe si faltan referencias.
    """
    existing = {p.name for p in spec.playbooks}
    missing = [ref for ref in DEFAULT_PLAYBOOK_REFS if ref["name"] not in existing]
    if not missing:
        return spec
    merged = spec.model_copy(update={"playbooks": [*spec.playbooks, *missing]})
    async with get_session() as session:
        row = (
            await session.execute(
                select(AgentSpecRecord).where(
                    AgentSpecRecord.tenant_id == tenant_id,
                    AgentSpecRecord.is_active.is_(True),
                    AgentSpecRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.spec = merged.model_dump(mode="json")
    return merged


async def ensure_default_spec(tenant_id: uuid.UUID) -> AgentSpec:
    """Crea (si falta) el AgentSpec default del tenant y lo activa."""
    spec_data = dict(DEFAULT_SPEC)
    spec_data["tenantId"] = str(tenant_id)
    async with get_session() as session:
        existing = (
            await session.execute(
                select(AgentSpecRecord).where(
                    AgentSpecRecord.tenant_id == tenant_id,
                    AgentSpecRecord.is_active.is_(True),
                    AgentSpecRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                AgentSpecRecord(
                    tenant_id=tenant_id,
                    version=DEFAULT_VERSION,
                    is_active=True,
                    spec=spec_data,
                )
            )
    return AgentSpec.model_validate(spec_data)


async def load_active_spec(tenant_id: uuid.UUID | None = None) -> AgentSpec:
    """Carga el AgentSpec activo del tenant. Crea el default si no existe."""
    tenant_id = tenant_id or get_current_tenant_id()
    if tenant_id is None:
        raise ValueError("No hay tenant para cargar el AgentSpec")

    async with get_session() as session:
        row = (
            await session.execute(
                select(AgentSpecRecord)
                .where(
                    AgentSpecRecord.tenant_id == tenant_id,
                    AgentSpecRecord.is_active.is_(True),
                    AgentSpecRecord.deleted_at.is_(None),
                )
                .order_by(AgentSpecRecord.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if row is None:
        return await ensure_default_spec(tenant_id)
    # Tolerancia v1→v2 (ADR-0050): rescatar escalación legacy antes de depurar.
    await _migrate_legacy_escalation(tenant_id, row.spec)
    spec = AgentSpec.model_validate(_drop_legacy_fields(row.spec))
    return await _ensure_default_playbooks(tenant_id, spec)