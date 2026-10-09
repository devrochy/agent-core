"""Una tool que pide relevo humano (`"escalate": True`) escala de verdad (BACKLOG Alta).

Caso real: `pack_commerce` devuelve `{"error": ..., "escalate": True}` cuando el pedido supera
`monto_alto_threshold`, y ningún punto del core leía esa clave: el pedido no pasaba a un humano.
Ahora el loop escala con el mecanismo nativo, corta el lote de tools y cierra el turno con la
respuesta de escalamiento. Con el stack real y un LLM falso.
"""

from __future__ import annotations

import os
import types
import uuid
from typing import Any

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

ERROR_MONTO = "El total supera el monto máximo permitido y requiere aprobación humana."


class _Respuesta:
    def __init__(self, stop_reason: str, content: str = "", tool_calls: list[Any] | None = None) -> None:
        self.stop_reason = stop_reason
        self.content = content
        self.tool_calls = tool_calls or []
        self.model = "fake"
        self.latency_ms = 1
        self.usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)


class _Proveedor:
    """Primera llamada: pide las tools dadas. Siguientes: responde texto."""

    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, tool_names: list[str]) -> None:
        self.calls = 0
        self.tool_names = tool_names

    async def generate(self, **kwargs: Any) -> _Respuesta:
        self.calls += 1
        if self.calls == 1:
            return _Respuesta(
                "tool_use",
                tool_calls=[
                    types.SimpleNamespace(id=f"c{i}", name=name, arguments={})
                    for i, name in enumerate(self.tool_names)
                ],
            )
        return _Respuesta("end_turn", content="Respuesta normal del modelo.")

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0


async def _negocio() -> uuid.UUID:
    # Negocio propio por test: `tenant_setup` es compartido y otros tests le cambian las
    # políticas del spec (p. ej. `maxToolCallsPerTurn`).
    from core.alta_negocio import onboard_tenant

    slug = f"escala-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Escala {slug}", slug=slug, persona={"tone": "cercano"})
    return tid


async def _conversacion(tid: uuid.UUID, channel: str = "whatsapp") -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel=channel, external_id=f"esc:{uuid.uuid4().hex[:8]}")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel=channel)
            session.add(conv)
            await session.flush()
            return conv.id


def _tools_falsas(monkeypatch, comportamiento: dict[str, Any]) -> list[str]:
    """`mcp_host.execute_tool` falso: cada nombre devuelve un dict o lanza una excepción."""
    from core.mcp.host import mcp_host

    ejecutadas: list[str] = []

    async def fake_execute(name, arguments, context):
        ejecutadas.append(name)
        accion = comportamiento[name]
        if isinstance(accion, Exception):
            raise accion
        return accion

    async def sin_denegacion(*_a, **_k):
        return None  # el habilitado de packs por negocio no es lo que se prueba aquí

    monkeypatch.setattr(mcp_host, "execute_tool", fake_execute)
    monkeypatch.setattr("core.orchestrator.loop._tool_denial", sin_denegacion)
    return ejecutadas


async def _estado(tid: uuid.UUID, conversation_id: uuid.UUID):
    from sqlalchemy import select

    from core.db.models import Conversation, Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            conv = await session.get(Conversation, conversation_id)
            escalaciones = (
                (await session.execute(select(Escalation).where(Escalation.conversation_id == conversation_id)))
                .scalars()
                .all()
            )
    return conv, escalaciones


async def _turno(tid, conversation_id, monkeypatch, proveedor):
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: proveedor)
    async with tenant_scope(tid):
        return await process_conversation(conversation_id, "quiero hacer el pedido")


async def test_una_tool_que_pide_escalar_escala_y_pausa_el_bot(monkeypatch) -> None:
    t1 = await _negocio()
    conversation_id = await _conversacion(t1)
    _tools_falsas(monkeypatch, {"crear_pedido": {"error": ERROR_MONTO, "escalate": True}})
    proveedor = _Proveedor(["crear_pedido"])

    result = await _turno(t1, conversation_id, monkeypatch, proveedor)

    assert result is not None and result.escalated
    assert "Respuesta normal" not in result.text
    assert proveedor.calls == 1, "tras escalar no se vuelve a llamar al LLM"
    conv, escalaciones = await _estado(t1, conversation_id)
    assert conv.status == "escalated" and conv.bot_paused is True
    assert [(e.reason, e.summary) for e in escalaciones] == [("plugin:crear_pedido", ERROR_MONTO)]


async def test_un_business_error_con_escalate_tambien_escala(monkeypatch) -> None:
    from core.shared.exceptions import BusinessError

    t1 = await _negocio()
    conversation_id = await _conversacion(t1)
    _tools_falsas(monkeypatch, {"crear_pedido": BusinessError(ERROR_MONTO, escalate=True)})

    result = await _turno(t1, conversation_id, monkeypatch, _Proveedor(["crear_pedido"]))

    assert result is not None and result.escalated
    _conv, escalaciones = await _estado(t1, conversation_id)
    assert [e.reason for e in escalaciones] == ["plugin:crear_pedido"]


async def test_tras_escalar_no_corren_las_demas_tools_del_lote(monkeypatch) -> None:
    t1 = await _negocio()
    conversation_id = await _conversacion(t1)
    ejecutadas = _tools_falsas(
        monkeypatch,
        {"crear_pedido": {"error": ERROR_MONTO, "escalate": True}, "generar_link_pago": {"status": "ok"}},
    )

    await _turno(t1, conversation_id, monkeypatch, _Proveedor(["crear_pedido", "generar_link_pago"]))

    assert ejecutadas == ["crear_pedido"]


async def test_un_error_normal_no_escala(monkeypatch) -> None:
    t1 = await _negocio()
    conversation_id = await _conversacion(t1)
    _tools_falsas(monkeypatch, {"crear_pedido": {"error": "Falta la dirección."}})

    result = await _turno(t1, conversation_id, monkeypatch, _Proveedor(["crear_pedido"]))

    assert result is not None and not result.escalated
    assert result.text == "Respuesta normal del modelo."
    conv, escalaciones = await _estado(t1, conversation_id)
    assert escalaciones == [] and not conv.bot_paused


async def test_escalate_tiene_que_ser_true_literal(monkeypatch) -> None:
    # Un valor "truthy" que no es `True` (p. ej. el texto "true") no escala.
    t1 = await _negocio()
    conversation_id = await _conversacion(t1)
    _tools_falsas(monkeypatch, {"crear_pedido": {"error": "x", "escalate": "true"}})

    result = await _turno(t1, conversation_id, monkeypatch, _Proveedor(["crear_pedido"]))

    assert result is not None and not result.escalated


async def test_en_el_playground_no_se_crea_escalacion(monkeypatch) -> None:
    from core.shared.test_mode import TEST_CHANNEL

    t1 = await _negocio()
    conversation_id = await _conversacion(t1, channel=TEST_CHANNEL)
    _tools_falsas(monkeypatch, {"crear_pedido": {"error": ERROR_MONTO, "escalate": True}})

    result = await _turno(t1, conversation_id, monkeypatch, _Proveedor(["crear_pedido"]))

    assert result is not None and result.escalated
    conv, escalaciones = await _estado(t1, conversation_id)
    assert escalaciones == [] and not conv.bot_paused


async def test_una_tool_declarativa_nunca_escala(monkeypatch) -> None:
    # Una tool declarativa devuelve lo que responde una API externa (invariante 7): aunque
    # esa respuesta traiga `"escalate": true` en el primer nivel, no pausa el bot.
    from core.mcp.declarative import PLUGIN_ID
    from core.mcp.types import MCPTool

    t1 = await _negocio()
    conversation_id = await _conversacion(t1)

    async def handler_externo(arguments, context):
        return {"escalate": True, "error": "respuesta de una API ajena"}

    monkeypatch.setattr(
        "core.mcp.declarative.tools_for_spec",
        lambda spec: [
            MCPTool(
                name="consultar_api_externa",
                description="Consulta una API externa del negocio.",
                parameters={"type": "object", "properties": {}},
                handler=handler_externo,
                plugin_id=PLUGIN_ID,
                scopes=[],
            )
        ],
    )
    _tools_falsas(monkeypatch, {})

    result = await _turno(t1, conversation_id, monkeypatch, _Proveedor(["consultar_api_externa"]))

    assert result is not None and not result.escalated
    conv, escalaciones = await _estado(t1, conversation_id)
    assert escalaciones == [] and not conv.bot_paused
