"""El verificador de afirmaciones dentro del loop (ADR-0119, HU-ARCH-29), con el
stack real y un LLM falso: reintento correctivo, acción del negocio al agotar los
reintentos (ADR-0092), link quitado sin reintento y una fila de veredicto por turno.
"""

from __future__ import annotations

import os
import re
import types
import uuid
from typing import Any

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


class _Respuesta:
    def __init__(self, stop_reason: str, content: str = "", tool_calls: list[Any] | None = None) -> None:
        self.stop_reason = stop_reason
        self.content = content
        self.tool_calls = tool_calls or []
        self.model = "fake"
        self.latency_ms = 1
        self.usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)


class _Guion:
    """LLM falso: pide `tools` en la primera llamada (si hay) y después responde los
    textos en orden (el último se repite). Guarda lo que recibió como mensaje de usuario."""

    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, textos: list[str], tools: list[str] | None = None) -> None:
        self.textos = textos
        self.tools = tools or []
        self.llamadas = 0
        self.instrucciones: list[str] = []

    async def generate(self, **kwargs: Any) -> _Respuesta:
        self.llamadas += 1
        ultimo = next((m for m in reversed(kwargs.get("messages") or []) if m.role == "user"), None)
        if ultimo is not None and str(ultimo.content).startswith("[SISTEMA]"):
            self.instrucciones.append(str(ultimo.content))
        if self.tools and self.llamadas == 1:
            return _Respuesta(
                "tool_use",
                tool_calls=[types.SimpleNamespace(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(self.tools)],
            )
        indice = min(self.llamadas - (2 if self.tools else 1), len(self.textos) - 1)
        return _Respuesta("end_turn", content=self.textos[indice])

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0


PEDIDO = re.compile(r"pedido\b(?:\W+\w+){0,3}\W+(?:confirmad\w*|cread\w*)", re.IGNORECASE)


async def _negocio(**behavior: Any) -> uuid.UUID:
    from sqlalchemy import select

    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"verif-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Verificador {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid):
        async with get_session() as session:
            row = (await session.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one_or_none()
            if row is None:
                row = AppSettings(tenant_id=tid, assistant_name="it")
                session.add(row)
            row.behavior_config = {**dict(row.behavior_config or {}), **behavior}
    return tid


async def _conversacion(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"verif:{uuid.uuid4().hex[:8]}")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp")
            session.add(conv)
            await session.flush()
            return conv.id


def _preparar(monkeypatch, guion: _Guion, tools: dict[str, dict] | None = None) -> None:
    from core.mcp.host import mcp_host
    from core.mcp.types import Afirmacion

    async def afirmaciones(_habilitados):
        return [Afirmacion(label="pedido", patron=PEDIDO, tools=frozenset({"crear_pedido"}))]

    async def ejecutar(name, arguments, context):
        return (tools or {})[name]

    async def sin_denegacion(*_a, **_k):
        return None

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    monkeypatch.setattr("core.orchestrator.loop.cargar_afirmaciones", afirmaciones)
    monkeypatch.setattr(mcp_host, "execute_tool", ejecutar)
    monkeypatch.setattr("core.orchestrator.loop._tool_denial", sin_denegacion)


async def _turno(tid: uuid.UUID, conversation_id: uuid.UUID):
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await process_conversation(conversation_id, "quiero hacer el pedido")


async def _estado(tid: uuid.UUID, conversation_id: uuid.UUID):
    from sqlalchemy import select

    from core.db.models import AuditLog, Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            escalaciones = (
                (await session.execute(select(Escalation).where(Escalation.conversation_id == conversation_id)))
                .scalars()
                .all()
            )
            veredictos = (
                (
                    await session.execute(
                        select(AuditLog.input).where(
                            AuditLog.conversation_id == conversation_id,
                            AuditLog.actor == "core:verificador",
                        )
                    )
                )
                .scalars()
                .all()
            )
    return escalaciones, veredictos


async def test_cierre_sin_accion_reintenta_y_escala_con_el_codigo(monkeypatch) -> None:
    tid = await _negocio(guardrail_max_retries=1)
    conversation_id = await _conversacion(tid)
    guion = _Guion(["Listo, tu pedido quedó confirmado."])
    _preparar(monkeypatch, guion)

    result = await _turno(tid, conversation_id)

    assert result is not None and result.escalated
    assert guion.llamadas == 2, "un reintento correctivo antes de escalar"
    assert len(guion.instrucciones) == 1 and "da por hecha una acción" in guion.instrucciones[0]
    escalaciones, veredictos = await _estado(tid, conversation_id)
    assert [(e.reason, e.summary) for e in escalaciones] == [("guardrail", "verificador.status_sin_evidencia")]
    assert len(veredictos) == 1
    assert veredictos[0]["resultado"] == "fallo" and veredictos[0]["intentos"] == 2
    assert veredictos[0]["motivos"] == ["status_sin_evidencia", "status_sin_evidencia"]


async def test_con_accion_neutral_no_escala(monkeypatch) -> None:
    tid = await _negocio(guardrail_max_retries=0, guardrail_fail_action="neutral")
    conversation_id = await _conversacion(tid)
    _preparar(monkeypatch, _Guion(["Listo, tu pedido quedó confirmado."]))

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert "pedido quedó confirmado" not in result.text
    escalaciones, _ = await _estado(tid, conversation_id)
    assert escalaciones == []


async def test_monto_corregido_en_el_reintento(monkeypatch) -> None:
    tid = await _negocio(guardrail_max_retries=1)
    conversation_id = await _conversacion(tid)
    guion = _Guion(["El total es $500.000.", "El total es $600.000."], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, {"generar_cotizacion": {"total": 600000, "status": "draft"}})

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == "El total es $600.000."
    assert "$600.000" in guion.instrucciones[0]
    _, veredictos = await _estado(tid, conversation_id)
    assert [(v["resultado"], v["motivos"]) for v in veredictos] == [("reintento", ["monto_sin_evidencia"])]


async def test_link_inventado_se_quita_y_llega_el_del_servidor(monkeypatch) -> None:
    tid = await _negocio()
    conversation_id = await _conversacion(tid)
    guion = _Guion(["Tu pedido quedó confirmado. Paga en https://pagos.ejemplo/inventado"], tools=["crear_pedido"])
    _preparar(
        monkeypatch,
        guion,
        {"crear_pedido": {"status": "confirmed", "_safe_append": "Link de pago: https://pagos.ejemplo/real"}},
    )

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert "inventado" not in result.text
    assert "https://pagos.ejemplo/real" in result.text
    assert guion.llamadas == 2, "quitar un link no consume reintento"
    _, veredictos = await _estado(tid, conversation_id)
    assert len(veredictos) == 1 and veredictos[0]["urls_quitadas"] == 1


async def test_turno_sin_afirmaciones_no_deja_veredicto(monkeypatch) -> None:
    tid = await _negocio()
    conversation_id = await _conversacion(tid)
    _preparar(monkeypatch, _Guion(["¡Hola! ¿En qué te ayudo?"]))

    await _turno(tid, conversation_id)

    _, veredictos = await _estado(tid, conversation_id)
    assert veredictos == []
