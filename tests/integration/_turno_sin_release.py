"""Turno sin release para los tests que miden la mecánica del motor, del verificador o de los guardrails (HU-AOS-90).

Antes de ADR-0206 esos tests corrían el turno sin release sobre el `AgentSpec` v2 de `agent_specs`, que ya no existe
(HU-AOS-91 parte D3). Este módulo arma el agente del turno (`AgenteDelTurno`) en el mismo punto del loop desde los
valores por defecto del negocio (`defaults_negocio.leer`), sus habilidades (`packs_del_negocio`) y los bindings que pida
el test, para que los tests sigan midiendo lo mismo. `instalar` parchea el loop; `transformar` cambia los valores por
defecto antes de armar el agente (p. ej. un modelo de respaldo).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest


async def agente_desde_el_negocio(
    transformar: Callable[[Any], Any] | None = None, *, bindings: Sequence[dict[str, Any]] = ()
) -> Any:
    from core.agents import defaults_negocio
    from core.agents.instalaciones_agente import packs_del_negocio
    from core.agents.turno import agente_desde_defaults
    from core.shared.tenant import get_current_tenant_id

    tid = get_current_tenant_id()
    assert tid is not None, "el turno sin release corre dentro del tenant_scope del negocio"
    defaults = await defaults_negocio.leer(tid)
    if transformar is not None:
        defaults = transformar(defaults)
    packs = sorted(await packs_del_negocio(tid, con_core=False))
    return con_bindings(agente_desde_defaults(defaults.model_dump(mode="json"), packs=packs, key="atencion"), bindings)


def con_bindings(agente: Any, bindings: Sequence[dict[str, Any]]) -> Any:
    """El agente del turno con estos bindings completos (`tool`, `enabled`, `scopes`, `maxCallsPerConversation`,
    `requiresConfirmation`): `agente_desde_defaults` solo copia `tool`, `enabled` y `scopes`."""
    import dataclasses

    from agentcore_contracts import AgentSpecV3

    if not bindings:
        return agente
    contenido = agente.v3.model_dump(mode="json", by_alias=True)
    contenido["tools"] = [dict(b) for b in bindings]
    return dataclasses.replace(agente, v3=AgentSpecV3.model_validate(contenido))


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
