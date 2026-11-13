"""Agentes base del negocio sin el General (ADR-0192, HU-AOS-68).

- `asegurar_coordinador(tenant_id)`: el Coordinador del negocio, creado al alta con su versión 1. Su
  contenido es el `KernelSpec` por defecto del contrato con la voz y el presupuesto de la
  configuración del negocio (ADR-0191 §4: Configuración siembra, no edita): sin texto de prompt en
  el código (invariante 3) y sin `routing.defaultAgent` (manda el `is_default` de la instancia).
- `contenido_base(spec)`: la base `agentspec/3` de un especialista nuevo, sacada de la configuración
  del negocio (persona, modelo, playbooks que no son de un pack, memoria y políticas). Sin tools ni
  procedimientos: los pone la plantilla del agente (`core.presets.agentes.derivar_contenido`).

Reemplazan al lector tolerante (`core/agents/tolerante.py`) en el alta y en los presets.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from agentcore_contracts import AgentSpec, AgentSpecV3, KernelSpec
from sqlalchemy import select, text

from core.agents.claves import KEY_COORDINADOR
from core.db.models import AgentRecord, AgentVersionRecord
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

NOMBRE_COORDINADOR = "Coordinador"
DESCRIPCION_COORDINADOR = "Coordinador de la conexión: decide qué agente atiende (ADR-0112)."


def _acotar(valor: Any, lo: float, hi: float) -> Any:
    if valor is None:
        return None
    return max(lo, min(hi, valor))


def kernel_del_negocio(spec: AgentSpec) -> dict[str, Any]:
    """`kernelspec/1` del Coordinador: defaults del contrato + voz y presupuesto del negocio."""
    persona = spec.persona
    voz: dict[str, Any] = {"tone": persona.tone, "warmth": persona.warmth, "emojis": persona.emojis}
    rol = persona.role.strip()[:200]
    if rol:
        voz["role"] = rol
    presupuesto = spec.policies.budgetUsdPerConversation
    kernel = KernelSpec.model_validate(
        {
            "voice": voz,
            "policies": {
                "budgetUsdPerConversation": _acotar(float(presupuesto), 0.0, 1000.0) if presupuesto is not None else None
            },
        }
    )
    return kernel.model_dump(mode="json", by_alias=True)


def contenido_base(spec: AgentSpec, *, playbooks_de_packs: Iterable[str] = ()) -> dict[str, Any]:
    """Base `agentspec/3` de un especialista nuevo desde la configuración del negocio (sin tools ni
    procedimientos: la completa la plantilla)."""
    from core.agents.spec import DEFAULT_SPEC

    de_packs = set(playbooks_de_packs)
    persona = spec.persona
    rag = spec.memory.rag
    primario = spec.model.primary.strip()[:100] or str(DEFAULT_SPEC["model"]["primary"])
    contenido: dict[str, Any] = {
        "persona": {
            "inheritConnectionVoice": True,
            "maxLength": _acotar(persona.maxLength, 0, 10_000),
            "forbiddenPhrases": [f[:1000] for f in persona.forbiddenPhrases][:100],
        },
        "model": {
            "primary": primario,
            "fallback": spec.model.fallback[:100] if spec.model.fallback else None,
            "router": spec.model.router[:100] if spec.model.router else None,
            "temperature": _acotar(float(spec.model.temperature), 0.0, 2.0),
        },
        "playbooks": [
            {"name": ref.name, "description": ref.description[:300]}
            for ref in spec.playbooks
            if ref.name not in de_packs
        ][:60],
        "procedures": [],
        "memory": {
            "shortTermTurns": _acotar(spec.memory.shortTermTurns, 1, 500),
            "rag": {"topK": _acotar(rag.topK, 1, 100), "minScore": _acotar(float(rag.minScore), 0.0, 1.0)}
            if rag is not None
            else None,
            # El perfil del cliente (ADR-0114): sin esto el agente no guardaría datos del cliente.
            "writeNamespaces": ["perfil"],
        },
        "tools": [],
        "guardrails": [],
        "declarativeTools": [],
        "policies": {"maxToolCallsPerTurn": _acotar(spec.policies.maxToolCallsPerTurn, 1, 100)},
    }
    return AgentSpecV3.model_validate(contenido).model_dump(mode="json", by_alias=True)


async def asegurar_coordinador(tenant_id: uuid.UUID, *, modules: Iterable[Any] | None = None) -> uuid.UUID:
    """El Coordinador del negocio con al menos una versión publicada. Idempotente; corre dentro del
    `tenant_scope` del negocio. Sin `versiones.crear_agente`: esa rechaza las keys reservadas."""
    from core.agents import versiones
    from core.agents.spec import leer_spec_activo
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("asegurar_coordinador: fuera del tenant_scope del negocio")
    async with get_session() as session:
        existente = (
            await session.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == tenant_id,
                    AgentRecord.key == KEY_COORDINADOR,
                    AgentRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            agent_id = existente.id
        else:
            agent_id = uuid.uuid4()
            creado = (
                await session.execute(
                    text(
                        "insert into agents (id, tenant_id, key, kind, name, description) "
                        "values (:id, :t, :key, 'core', :name, :desc) "
                        "on conflict (tenant_id, key) where deleted_at is null do nothing returning id"
                    ),
                    {"id": agent_id, "t": tenant_id, "key": KEY_COORDINADOR, "name": NOMBRE_COORDINADOR,
                     "desc": DESCRIPCION_COORDINADOR},
                )
            ).scalar_one_or_none()
            if creado is None:  # otra alta concurrente lo creó primero
                agent_id = (
                    await session.execute(
                        select(AgentRecord.id).where(
                            AgentRecord.tenant_id == tenant_id,
                            AgentRecord.key == KEY_COORDINADOR,
                            AgentRecord.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
        con_version = (
            await session.execute(
                select(AgentVersionRecord.id)
                .where(AgentVersionRecord.tenant_id == tenant_id, AgentVersionRecord.agent_id == agent_id)
                .limit(1)
            )
        ).scalar_one_or_none()
    if con_version is None:
        leido = await leer_spec_activo(tenant_id)
        contenido = kernel_del_negocio(leido[1]) if leido else KernelSpec().model_dump(mode="json", by_alias=True)
        del leido
        modulos = list(modules) if modules is not None else plugin_manager.list_modules()
        await versiones.publicar(
            tenant_id,
            agent_id,
            catalogo=catalogo_de_acciones(modulos),
            contenido=contenido,
            label="alta",
            notes="Coordinador del alta (ADR-0192): defaults del contrato y la voz del negocio.",
        )
        log.info("coordinador_creado", tenant_id=str(tenant_id), agent_id=str(agent_id))
    return agent_id


__all__ = ["asegurar_coordinador", "contenido_base", "kernel_del_negocio"]
