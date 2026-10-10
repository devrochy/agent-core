"""HU-SH-07 · Aceptar los términos del alta por el loop real (ADR-0125), sin LLM.

El defecto del journey 05 (2026-10-09) no se veía llamando la tool directo: entre turnos el
modelo solo ve texto, así que en el turno en que el cliente acepta llama `crear_pedido` primero
sin el token (o con `true`) y después con el token que le devuelve esa llamada. Acá un modelo
guionado hace exactamente eso, a través del runner de journeys (`paso_usuario`: guarda el
mensaje del cliente como el consumidor y corre `process_conversation`) y de la tool real de
commerce.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from test_antiabuso_alta import _cotizacion, _datos, _vendedor

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
assert _SPEC and _SPEC.loader
journeys = importlib.util.module_from_spec(_SPEC)
sys.modules["journeys"] = journeys
_SPEC.loader.exec_module(journeys)

ACEPTA = "Sí, acepto los términos, el aviso de privacidad y el DPA."


class _Respuesta:
    def __init__(self, stop_reason: str, content: str = "", tool_calls: list[Any] | None = None) -> None:
        self.stop_reason = stop_reason
        self.content = content
        self.tool_calls = tool_calls or []
        self.model = "guion"
        self.latency_ms = 1
        self.usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)


class _ModeloGuionado:
    """Lo que hace un modelo real en el journey 05.

    - Turno en que pide el plan: `crear_pedido` sin token y, con los enlaces, pide aceptar.
    - Turno en que el cliente acepta: el token quedó en un resultado de tool del turno anterior,
      que el historial no trae; llama sin token, después con `true` y por último con el token que
      le devolvió la llamada anterior de este mismo turno.
    """

    provider_name = "guion"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, quote_id: str, datos: dict[str, Any]) -> None:
        self.quote_id = quote_id
        self.datos = datos
        self.llamadas: list[dict[str, Any]] = []

    def _crear_pedido(self, **extra: Any) -> _Respuesta:
        argumentos = {
            "quote_id": self.quote_id,
            "payment_method": "transferencia",
            "customer_data": {**self.datos, **extra},
        }
        self.llamadas.append(argumentos)
        llamada = types.SimpleNamespace(id=f"c-{uuid.uuid4().hex[:8]}", name="crear_pedido", arguments=argumentos)
        return _Respuesta("tool_use", tool_calls=[llamada])

    async def generate(self, **kwargs: Any) -> _Respuesta:
        mensajes = kwargs.get("messages") or []
        ultimo = max(i for i, m in enumerate(mensajes) if m.role == "user")
        resultados = [json.loads(m.content) for m in mensajes[ultimo + 1 :] if m.role == "tool"]
        if "acepto" not in str(mensajes[ultimo].content).lower():
            if not resultados:
                return self._crear_pedido()
            return _Respuesta("end_turn", content="Antes de seguir, revisa y acepta los términos, la privacidad y el DPA.")
        if not resultados:
            return self._crear_pedido()
        if len(resultados) == 1:
            return self._crear_pedido(acepta_terminos=True)
        if len(resultados) == 2:
            return self._crear_pedido(acepta_terminos=resultados[-1]["acepta_terminos"])
        return _Respuesta("end_turn", content="Gracias, seguimos con tu alta.")

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0


@pytest.fixture
async def vendedor(monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    from core.config import get_settings

    await journeys._preparar_proceso()  # packs activos y tools registradas, como el runner
    tid = await _vendedor()
    monkeypatch.setenv("PROVISIONING_SELLER_TENANTS", str(tid))
    get_settings.cache_clear()
    yield tid
    monkeypatch.delenv("PROVISIONING_SELLER_TENANTS", raising=False)
    get_settings.cache_clear()


async def _salidas_de_crear_pedido(ctx: Any) -> list[dict[str, Any]]:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            filas = (
                await s.execute(
                    select(AuditLog.output)
                    .where(
                        AuditLog.tenant_id == ctx.tenant_id,
                        AuditLog.conversation_id == ctx.conversation_id,
                        AuditLog.actor == "tool:crear_pedido",
                        AuditLog.action == "execute",
                    )
                    .order_by(AuditLog.created_at)
                )
            ).scalars().all()
    return list(filas)


async def test_el_cliente_acepta_en_el_turno_siguiente_y_el_alta_queda_confirmada(vendedor, monkeypatch) -> None:
    from core.db.models import Message
    from core.db.session import get_session
    from core.provisioning.antiabuso import pedido_de_alta
    from core.shared.tenant import tenant_scope

    await journeys.publicar_documentos_legales(
        vendedor,
        [
            {"tipo": tipo, "version": "journeys", "url": f"https://solvershub.test/legal/{tipo}"}
            for tipo in ("terminos", "privacidad", "dpa")
        ],
    )
    customer_id, conversation_id = await journeys.nuevo_cliente(vendedor, "Ana")
    ctx = journeys.Contexto(vendedor, customer_id, conversation_id, journeys.PagoSimulado())
    datos = _datos(id_document=ctx.valor("cliente.documento"), phone=ctx.valor("cliente.celular"))
    modelo = _ModeloGuionado(await _cotizacion(vendedor, customer_id), datos)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: modelo)
    registro: list[str] = []

    await journeys.paso_usuario(
        ctx, {"usuario": "Quiero activar el Plan Free con mis datos.", "hasta_herramienta": "crear_pedido"}, registro
    )
    assert [s["status"] for s in await _salidas_de_crear_pedido(ctx)] == ["terminos_pendientes"]

    await journeys.paso_usuario(ctx, {"usuario": ACEPTA, "hasta_herramienta": "crear_pedido"}, registro)

    salidas = await _salidas_de_crear_pedido(ctx)
    assert [s["status"] for s in salidas] == [
        "terminos_pendientes", "terminos_pendientes", "terminos_pendientes", "confirmed",
    ], registro
    assert len({s["acepta_terminos"] for s in salidas[:3]}) == 1, "el token es el mismo en cada llamada"
    assert [ll["customer_data"].get("acepta_terminos") for ll in modelo.llamadas] == [
        None, None, True, salidas[0]["acepta_terminos"],
    ]
    async with tenant_scope(vendedor):
        async with get_session() as s:
            mensajes = (
                await s.execute(
                    select(Message.id, Message.content)
                    .where(Message.tenant_id == vendedor, Message.conversation_id == conversation_id)
                    .order_by(Message.created_at)
                )
            ).all()
        registro_alta = await pedido_de_alta(vendedor, salidas[-1]["public_order_number"])
    # El runner guarda cada mensaje del cliente, como el consumidor.
    assert [m.content for m in mensajes] == ["Quiero activar el Plan Free con mis datos.", ACEPTA]
    assert registro_alta.customer_id == customer_id
    assert registro_alta.aceptacion["mensaje_id"] == str(mensajes[-1].id)
    assert {d["version"] for d in registro_alta.aceptacion["documentos"]} == {"journeys"}

