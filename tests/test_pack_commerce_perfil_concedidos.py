"""Las tools de Ventas escriben el perfil con los `writeNamespaces` del agente (ADR-0114 §3, ADR-0179 §10).

Hallazgo del auditor multitenant sobre agent-core#816: `crear_pedido` escribía el perfil directo, saltándose la
concesión y la puerta del aviso de datos. El pedido se guarda igual; sin `perfil` concedido solo se omite la copia al
perfil. Las tools de lead son del core desde ADR-0193: su caso está en `tests/test_leads_perfil_concedidos.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pack_commerce import skills
from pack_commerce.skills import _guardar_en_perfil

from core.mcp.types import ToolContext
from core.memory.facts import HechoInvalido, NamespaceNoConcedido


def _ctx(write_namespaces: frozenset[str] | None) -> ToolContext:
    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
        agent_id=uuid.uuid4(),
        write_namespaces=write_namespaces,
    )


class _Guardar:
    """Doble de `datos_cliente.guardar` que anota cada llamada y opcionalmente falla."""

    def __init__(self, error: Exception | None = None) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.error = error

    async def __call__(self, customer_id: uuid.UUID, atributos: dict[str, Any], **kw: Any) -> None:
        self.llamadas.append({"customer_id": customer_id, "atributos": dict(atributos), **kw})
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_pasa_los_concedidos_y_el_agente_del_contexto(monkeypatch: pytest.MonkeyPatch) -> None:
    doble = _Guardar()
    monkeypatch.setattr(skills, "guardar_datos", doble)
    ctx = _ctx(frozenset({"perfil"}))

    await _guardar_en_perfil(ctx, {"name": "Ana", "email": None, "phone": "  "}, accion="ventas.pedido_retiro")

    [llamada] = doble.llamadas
    assert llamada["concedidos"] == frozenset({"perfil"})
    assert llamada["agent_id"] == ctx.agent_id and llamada["customer_id"] == ctx.customer_id
    assert llamada["accion"] == "ventas.pedido_retiro" and llamada["source"] == "tool"
    # Los vacíos no pisan lo que el perfil ya tenía.
    assert llamada["atributos"] == {"name": "Ana"}


@pytest.mark.asyncio
async def test_sin_nada_que_guardar_no_llama(monkeypatch: pytest.MonkeyPatch) -> None:
    doble = _Guardar()
    monkeypatch.setattr(skills, "guardar_datos", doble)
    await _guardar_en_perfil(_ctx(frozenset({"perfil"})), {"email": None, "address": ""})
    assert doble.llamadas == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [NamespaceNoConcedido("namespace no concedido: perfil"), HechoInvalido("x"), RuntimeError("bd")])
async def test_la_negativa_no_rompe_la_tool(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(skills, "guardar_datos", _Guardar(error))
    await _guardar_en_perfil(_ctx(frozenset()), {"name": "Ana"})
