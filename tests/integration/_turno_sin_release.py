"""Turno sin release para los tests que miden la mecánica del motor, del verificador o de los guardrails (HU-AOS-90).

Antes de ADR-0206 esos tests corrían el turno sin release sobre el `AgentSpec` v2 de `agent_specs`. El turno ya no
lee ese v2: este módulo arma el agente del turno (`AgenteDelTurno`) desde la configuración del negocio en el mismo
punto del loop, para que los tests sigan midiendo lo mismo. `instalar` parchea el loop; `transformar` cambia el v2
antes de convertirlo (p. ej. un modelo de respaldo).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest


async def agente_desde_el_negocio(transformar: Callable[[Any], Any] | None = None) -> Any:
    from agentcore_contracts import AgentSpecV3

    from core.agents.base import contenido_base, kernel_del_negocio
    from core.agents.spec import load_active_spec
    from core.agents.turno import agente_del_turno
    from core.shared.release_vigente import AgenteCongelado

    spec = await load_active_spec()
    if transformar is not None:
        spec = transformar(spec)
    contenido = contenido_base(spec)
    contenido["model"] = spec.model.model_dump(mode="json")
    contenido["tools"] = [
        {
            "tool": b.tool,
            "enabled": b.enabled,
            "scopes": list(b.scopes),
            "maxCallsPerConversation": b.maxCallsPerConversation,
            "requiresConfirmation": b.requiresConfirmation,
        }
        for b in spec.tools
    ]
    contenido = AgentSpecV3.model_validate(contenido).model_dump(mode="json", by_alias=True)

    def _congelado(rol: str, key: str, content: dict[str, Any], contrato: str) -> AgenteCongelado:
        return AgenteCongelado(
            instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=key, role=rol, version_id=uuid.uuid4(),
            version=1, contract=contrato, content=content,
        )

    especialista = _congelado("specialist", "atencion", contenido, "agentspec/3")
    kernel = _congelado("core", "coordinador", kernel_del_negocio(spec), "kernelspec/1")
    return agente_del_turno(especialista, kernel, skills=tuple(spec.packs), tenant_plugins=())


def instalar(monkeypatch: pytest.MonkeyPatch, transformar: Callable[[Any], Any] | None = None) -> None:
    """El turno corre sin release y con el agente del turno armado desde el negocio (ver el docstring)."""
    estado: dict[str, Any] = {}

    async def _sin_release(*_a: Any, **_k: Any) -> None:
        return None

    async def _sin_compilado() -> None:
        estado["agente"] = await agente_desde_el_negocio(transformar)
        return None

    monkeypatch.setattr("core.orchestrator.loop.fijar_release_del_turno", _sin_release)
    monkeypatch.setattr("core.orchestrator.loop._compilado_del_turno", _sin_compilado)
    monkeypatch.setattr("core.orchestrator.loop.del_turno", lambda: estado.get("agente"))
