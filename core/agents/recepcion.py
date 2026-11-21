"""Vista de recepción del Coordinador (ADR-0205 §5 y §6, HU-AOS-89).

Cuando la release de la conexión no tiene especialistas, el foco del turno es el Coordinador (HU-AOS-88). Para que
el turno corra por el camino de siempre (vista del turno, compilado, concesiones y ledger), el core lo sustituye
por esta vista: un `AgenteCongelado` armado en memoria, nunca persistido ni editable, cuyo `content` es un
`agentspec/3` válido con el set cerrado de tools de recepción.

- Conserva los ids reales del Coordinador (`instance_id`, `agent_id`, `version_id`): el `usage_ledger` y la
  procedencia de los registros tienen FK a `agent_versions`.
- El modelo del contenido es el de la plataforma: en el turno manda `AppSettings.llm_model` cuando el negocio
  tiene uno (`_resolved_model` del loop), sin cambiar el contrato de `KernelSpec` (ADR-0205 §5).
- Las instrucciones son el playbook del kernel `recepcion-coordinador` (invariante 3): el prompt builder lo agrega
  cuando el agente es la vista. `respuestas` va en la allowlist para los textos de escalamiento y cierre.
- Sin packs, sin procedimientos y con el flujo vacío: el Coordinador no cotiza, no agenda, no cobra ni inscribe.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from core.shared.release_vigente import AgenteCongelado

# ADR-0205 §5: el set cerrado de tools del Coordinador cuando atiende (decisión del dueño del 2026-11-20).
TOOLS_RECEPCION = (
    "crear_lead",
    "get_customer_profile",
    "update_customer_profile",
    "pedir_telefono",
    "responder_aviso_datos",
    "escalate_to_human",
    "set_conversation_note",
)
# `update_customer_profile` desde la recepción: solo los datos de contacto (lo hace cumplir el host, no la tool).
ATRIBUTOS_DE_PERFIL = frozenset({"name", "phone", "email"})
PLAYBOOK_RECEPCION = "recepcion-coordinador"
# Prefijo de `AgentSpec.version` en la vista: identifica los turnos de recepción en el `usage_ledger`.
VERSION_RECEPCION = "coordinador@recepcion"
_FLUJO_VACIO = {"sha256": "recepcion", "sections": []}


def contenido_de_recepcion(modelo: str) -> dict[str, Any]:
    """El `agentspec/3` de la vista, validado por el contrato."""
    from agentcore_contracts import AgentSpecV3

    contenido = {
        "persona": {"inheritConnectionVoice": True},
        "model": {"primary": modelo},
        "playbooks": [{"name": "respuestas"}],
        "procedures": [],
        "memory": {"writeNamespaces": ["perfil"]},
        "tools": [{"tool": tool, "enabled": True, "scopes": []} for tool in TOOLS_RECEPCION],
        "guardrails": [],
        "declarativeTools": [],
    }
    return AgentSpecV3.model_validate(contenido).model_dump(mode="json", by_alias=True)


def vista_de_recepcion(coordinador: AgenteCongelado, *, modelo: str | None = None) -> AgenteCongelado:
    """La vista del Coordinador para atender. Con un agente que no es el Coordinador, lo devuelve igual."""
    if coordinador.role != "core":
        return coordinador
    if modelo is None:
        from core.config import get_settings

        modelo = get_settings().llm_model
    return dataclasses.replace(
        coordinador,
        contract="agentspec/3",
        content=contenido_de_recepcion(modelo),
        procedimientos=(),
        packs=frozenset(),
        flow=_FLUJO_VACIO,
    )


def es_vista_de_recepcion(agente: Any) -> bool:
    """El agente del turno es la vista de recepción (el Coordinador atendiendo)."""
    return agente is not None and getattr(agente, "role", None) == "core" and getattr(agente, "contract", "") == "agentspec/3"


def atributos_de_recepcion(argumentos: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Los argumentos de `update_customer_profile` recortados a los datos de contacto, y las claves descartadas."""
    atributos = argumentos.get("attributes")
    if not isinstance(atributos, dict):
        # Falla cerrado: la tool aceptaría una lista de pares (`dict([...])`) y el host no valida el esquema.
        return {**argumentos, "attributes": {}}, ["attributes"] if atributos else []
    fuera = sorted(k for k in atributos if k not in ATRIBUTOS_DE_PERFIL)
    if not fuera:
        return argumentos, []
    return {**argumentos, "attributes": {k: v for k, v in atributos.items() if k in ATRIBUTOS_DE_PERFIL}}, fuera
