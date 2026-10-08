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
from sqlalchemy.exc import IntegrityError

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


def enabled_business_packs(spec: AgentSpec) -> set[str]:
    """Packs de NEGOCIO del tenant, SIN el core.

    Es la cuenta que consumen las cuotas de plan (ADR-0045, métrica `plugins`) y
    el KPI del panel: el core es plomería del framework, no un pack que el tenant
    habilite, así que no consume cuota ni se lista en `/admin/plugins`.
    Fuente única para que la métrica del plan y el dashboard no diverjan.
    """
    return set(spec.packs)


def count_tenant_packs(spec: AgentSpec) -> int:
    """Cuántos packs de negocio tiene habilitados el tenant (sin el core)."""
    return len(enabled_business_packs(spec))


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
    "model": {"primary": "deepseek-flash", "temperature": 0.3},
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
    # Playbooks de packs habilitados del tenant (ADR-0019): un playbook nuevo en
    # un pack (p. ej. `citas` de pack-calendar) entra al allowlist sin tocar el
    # spec de cada tenant -- data migration perezosa, idempotente. Import local
    # para no crear un ciclo (onboarding importa spec a nivel de módulo).
    try:
        from core.onboarding import _pack_playbook_refs

        for ref in _pack_playbook_refs(list(spec.packs)):
            if ref["name"] not in existing and ref["name"] not in {m["name"] for m in missing}:
                missing.append(ref)
    except Exception:
        pass
    if not missing:
        return spec
    # `model_validate`, no `model_copy(update=...)`: model_copy no valida y los `missing`
    # (dicts) quedaban como dicts en el spec devuelto; el primer turno tras sumarse un
    # playbook de pack se caía en `load_playbook` (`'dict' object has no attribute
    # 'name'`). Visto con los journeys de HU-SUB-12.
    merged = AgentSpec.model_validate(
        {**spec.model_dump(mode="json"), "playbooks": [*spec.model_dump(mode="json")["playbooks"], *missing]}
    )
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


async def activate_spec_version(
    tenant_id: uuid.UUID, version: str | None = None, *, record_id: uuid.UUID | None = None
) -> str:
    """Activa una versión del AgentSpec del tenant (rollback/activación, ADR-0051).

    Sin `version` ni `record_id`, revierte a la versión inmediatamente anterior a la
    activa. Desactiva la actual e inserta el flip en la misma transacción (índice
    único: una sola activa por tenant). Devuelve la versión activada. Lanza
    `ValueError` si no existe. Fuente única para el CLI y el panel.
    """
    async with get_session() as session:
        rows = sorted(
            (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalars().all(),
            key=lambda r: r.created_at,
        )
        if not rows:
            raise ValueError("Sin AgentSpec para el tenant")
        current = next((r for r in rows if r.is_active), rows[-1])

        if record_id is not None:
            target = next((r for r in rows if r.id == record_id), None)
        elif version is not None:
            target = next((r for r in rows if r.version == version), None)
        else:
            target = next((r for r in reversed(rows) if r.created_at < current.created_at), None)
            if target is None:
                raise ValueError("No hay versión anterior para revertir")

        if target is None:
            raise ValueError(f"Versión no existe: {version or record_id}")
        if target.is_active:
            return target.version

        # Dos fases (el índice único parcial exige que no haya dos activas a la vez).
        for r in rows:
            r.is_active = False
        await session.flush()
        target.is_active = True
    return target.version

async def next_spec_version(tenant_id: uuid.UUID) -> str:
    """Versión nueva: sube la patch de la más alta existente (1.0.0 → 1.0.1)."""
    async with get_session() as session:
        versions = (
            await session.execute(
                select(AgentSpecRecord.version).where(
                    AgentSpecRecord.tenant_id == tenant_id,
                    AgentSpecRecord.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    nums: list[list[int]] = []
    for v in versions:
        parts = [int(x) for x in str(v).split(".") if x.isdigit()]
        if parts:
            nums.append(parts)
    if not nums:
        return "1.0.0"
    base = max(nums, key=lambda p: tuple(([*p, 0, 0])[:3]))
    base = ([*base, 0, 0])[:3]
    base[2] += 1
    return ".".join(str(x) for x in base)


async def save_spec_version(
    new_spec: AgentSpec, tenant_id: uuid.UUID | None = None
) -> str | None:
    """Persiste `new_spec` como nueva versión ACTIVA del tenant.

    Desactiva la versión previa e inserta la nueva en la misma transacción.
    Fuente única para el panel SSR (`_save_spec_version`) y la API `/api/v1`
    (ADR-0076/0077). Devuelve la versión nueva, o `None` si hubo una edición
    concurrente (los índices parciales protegen la integridad).
    """
    tid = tenant_id or get_current_tenant_id()
    if tid is None:
        return None
    version = await next_spec_version(tid)
    try:
        async with get_session() as session:
            current_row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tid,
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if current_row is not None:
                current_row.is_active = False
            session.add(
                AgentSpecRecord(
                    tenant_id=tid,
                    version=version,
                    is_active=True,
                    spec=new_spec.model_dump(mode="json"),
                )
            )
    except IntegrityError:
        return None
    return version


async def save_spec_if_changed(
    current: AgentSpec, data: dict[str, Any], tenant_id: uuid.UUID | None = None
) -> str | None:
    """Guarda una versión nueva SOLO si `data` difiere de `current`.

    Evita crear una versión en cada guardado cuando lo único que cambió fue
    `AppSettings` (nombre, tono, debounce, etc.). Fuente única para el panel SSR
    y la API.
    """
    try:
        new_spec = AgentSpec.model_validate(data)
    except Exception:
        return None
    if new_spec.model_dump(mode="json") == current.model_dump(mode="json"):
        return None
    return await save_spec_version(new_spec, tenant_id)
