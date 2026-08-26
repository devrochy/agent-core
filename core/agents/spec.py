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

DEFAULT_VERSION = "1.0.0"

# Spec default SIN texto de prompts (ADR-0014): identidad/tono/reglas viven en los
# playbooks referenciados por `playbooks`, no aquí.
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
        "locale": "es-CO",
    },
    "model": {"primary": "deepseek-chat", "temperature": 0.3},
    "memory": {"shortTermTurns": 30, "summarize": True},
    "playbooks": [
        {"name": "identidad", "description": "Identidad del asistente"},
        {"name": "tono", "description": "Voz y tono del asistente"},
        {"name": "reglas-operacion", "description": "Reglas de operación del asistente"},
    ],
    "tools": [],
    "policies": {
        "escalation": {
            "triggers": ["asesor", "humano", "persona real", "reclamo", "queja"]
        },
        "confirmBeforeWrite": True,
        "maxToolCallsPerTurn": 10,
        "budgetUsdPerConversation": 0.5,
    },
    "packs": [],
}


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
    return AgentSpec.model_validate(row.spec)