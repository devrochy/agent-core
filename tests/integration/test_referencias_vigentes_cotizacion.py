"""Referencias vigentes de la conversación con pack-commerce real (ADR-0130).

Incidente (test, tenant solvershub, 2026-10-10): el historial que relee el modelo es
solo texto, así que en el turno siguiente no tenía el `quote_id` real. Llamó
`crear_pedido(quote_id="PLAN-PREMIUM")`, `confirmar_cotizacion("OC-0001")` y
`confirmar_cotizacion("Q-0001")`, generó 6 cotizaciones en 9 minutos y nunca cerró
el pedido.

- La fuente del pack trae solo la cotización en curso y el último pedido de ESTA
  conversación y de ESTE cliente.
- Un `quote_id` que no existe devuelve `quote_id_vigente`, sin usarla en silencio.
- Journey de varios turnos con el loop real y un modelo guionado (sin LLM real) que
  toma el `quote_id` del bloque de referencias del prompt.
"""

from __future__ import annotations

import json
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

PRECIO = 600000.0
DATOS = {"full_name": "Ana Gómez", "id_document": "1012345678", "phone": "3001234567", "email": "ana@example.com"}
_QUOTE_EN_PROMPT = re.compile(r"- cotizacion: quote_id=([0-9a-f-]{36})")


# ------------------------------------------------------------------ negocio y datos
async def _negocio() -> tuple[uuid.UUID, str]:
    """Negocio propio con pack-commerce y un plan Premium; devuelve (tenant, variant_id)."""
    from pack_commerce.models import Product, ProductVariant

    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"refs-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Referencias {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False
    )
    async with tenant_scope(tid):
        async with get_session() as s:
            producto = Product(tenant_id=tid, name="Plan Premium")
            s.add(producto)
            await s.flush()
            variante = ProductVariant(
                tenant_id=tid, product_id=producto.id, sku="PLAN-PREMIUM", price=PRECIO, billing_period="mensual"
            )
            s.add(variante)
            await s.flush()
            return tid, str(variante.id)


async def _conversacion(tid: uuid.UUID, customer_id: uuid.UUID | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            if customer_id is None:
                customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"refs:{uuid.uuid4().hex[:8]}")
                s.add(customer)
                await s.flush()
                customer_id = customer.id
            conv = Conversation(tenant_id=tid, customer_id=customer_id, channel="whatsapp")
            s.add(conv)
            await s.flush()
            return conv.id, customer_id


async def _cotizacion(tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID, variant_id: str, status: str = "draft") -> str:
    from pack_commerce.models import Quote, QuoteItem

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            q = Quote(tenant_id=tid, conversation_id=conv, customer_id=cliente,
                      subtotal=PRECIO, discount=0, total=PRECIO, status=status)
            s.add(q)
            await s.flush()
            s.add(QuoteItem(tenant_id=tid, quote_id=q.id, variant_id=uuid.UUID(variant_id), quantity=1, unit_price=PRECIO))
            await s.flush()
            return str(q.id)


def _ctx(conv: uuid.UUID, cliente: uuid.UUID):  # type: ignore[no-untyped-def]
    from core.mcp.types import ToolContext

    return ToolContext(conversation_id=conv, customer_id=cliente,
                       idempotency_key=f"t:{uuid.uuid4().hex[:8]}", channel="whatsapp")


async def _lineas(tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID) -> list[str]:
    from pack_commerce.skills import referencias_vigentes

    from core.orchestrator.referencias import renderizar
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return [renderizar(r) or "" for r in await referencias_vigentes(conv, cliente)]


# ------------------------------------------------------------------ fuente y tools
async def test_solo_la_cotizacion_de_esta_conversacion_y_de_este_cliente() -> None:
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    otra_conv, _ = await _conversacion(tid, cliente)
    _, otro_cliente = await _conversacion(tid)

    propia = await _cotizacion(tid, conv, cliente, variante)
    # Más nuevas, pero de otra conversación del mismo cliente o de otro cliente.
    await _cotizacion(tid, otra_conv, cliente, variante)
    await _cotizacion(tid, conv, otro_cliente, variante)

    assert await _lineas(tid, conv, cliente) == [f"- cotizacion: quote_id={propia}, estado=draft, total=600000.00"]


async def test_otro_negocio_no_ve_la_cotizacion() -> None:
    tid, variante = await _negocio()
    otro_tid, _ = await _negocio()
    conv, cliente = await _conversacion(tid)
    await _cotizacion(tid, conv, cliente, variante)

    assert await _lineas(otro_tid, conv, cliente) == []


async def test_una_cotizacion_convertida_ya_no_es_vigente() -> None:
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    await _cotizacion(tid, conv, cliente, variante, status="convertida")

    assert await _lineas(tid, conv, cliente) == []


@pytest.mark.parametrize("quote_id", ["PLAN-PREMIUM", "OC-0001", "Q-0001", str(uuid.uuid4())])
async def test_un_id_que_no_existe_devuelve_la_vigente_sin_usarla(quote_id: str) -> None:
    from pack_commerce.models import Quote
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    vigente = await _cotizacion(tid, conv, cliente, variante)

    async with tenant_scope(tid):
        confirmar = await OrderSkill().confirmar_cotizacion({"quote_id": quote_id}, _ctx(conv, cliente))
        pedido = await OrderSkill().crear_pedido(
            {"quote_id": quote_id, "payment_method": "transferencia", "customer_data": DATOS}, _ctx(conv, cliente)
        )
        async with get_session() as s:
            estado = (await s.get(Quote, uuid.UUID(vigente))).status

    for resultado in (confirmar, pedido):
        assert resultado["quote_id_vigente"] == vigente, resultado
        assert resultado["total_vigente"] == PRECIO
        assert "generar_cotizacion" not in json.dumps(resultado, ensure_ascii=False)
    assert estado == "draft", "la vigente no se confirma en silencio"


async def test_sin_cotizacion_vigente_el_error_de_siempre() -> None:
    from pack_commerce.skills import OrderSkill, _sin_cotizacion_vigente

    from core.shared.tenant import tenant_scope

    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    otra_conv, _ = await _conversacion(tid, cliente)
    await _cotizacion(tid, otra_conv, cliente, variante)  # de otra conversación: no cuenta

    async with tenant_scope(tid):
        resultado = await OrderSkill().crear_pedido(
            {"quote_id": "PLAN-PREMIUM", "payment_method": "transferencia", "customer_data": DATOS}, _ctx(conv, cliente)
        )
    # Sin cotización vigente, el error de siempre con la pista de no inventar el quote_id (#414).
    assert resultado == _sin_cotizacion_vigente()
    assert "quote_id_vigente" not in resultado


# ------------------------------------------------------------------ journey con el loop
class _Respuesta:
    def __init__(self, stop_reason: str, content: str = "", tool_calls: list[Any] | None = None) -> None:
        self.stop_reason = stop_reason
        self.content = content
        self.tool_calls = tool_calls or []
        self.model = "fake"
        self.latency_ms = 1
        self.usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)


class _Comprador:
    """Modelo guionado de una compra en varios turnos. El `quote_id` lo toma del bloque
    de referencias del prompt; si no está, adivina como en el incidente ("PLAN-PREMIUM").
    Si una tool responde `quote_id_vigente`, reintenta una vez con ese id."""

    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, variant_id: str) -> None:
        self.variant_id = variant_id
        self.ids_usados: list[str] = []
        self.prompts: list[str] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    @staticmethod
    def _tool(name: str, **arguments: Any) -> _Respuesta:
        return _Respuesta("tool_use", tool_calls=[types.SimpleNamespace(id=uuid.uuid4().hex, name=name, arguments=arguments)])

    def _quote_id(self, system_blocks: list[str]) -> str:
        encontrado = _QUOTE_EN_PROMPT.search(system_blocks[-1])
        return encontrado.group(1) if encontrado else "PLAN-PREMIUM"

    async def generate(self, **kwargs: Any) -> _Respuesta:
        messages = kwargs["messages"]
        system_blocks = kwargs["system_blocks"]
        ultimo = messages[-1]
        if ultimo.role == "tool":
            resultado = json.loads(ultimo.content)
            llamada = messages[-2].tool_calls[0]
            if resultado.get("quote_id_vigente") and llamada.arguments.get("quote_id") != resultado["quote_id_vigente"]:
                self.ids_usados.append(resultado["quote_id_vigente"])
                return self._tool(llamada.name, **{**llamada.arguments, "quote_id": resultado["quote_id_vigente"]})
            if resultado.get("error"):
                return _Respuesta("end_turn", content="No pude completar ese paso. ¿Lo intentamos de nuevo?")
            return _Respuesta("end_turn", content={
                "generar_cotizacion": "Tu cotización del Plan Premium da $600.000 al mes. ¿Confirmás ese total?",
                "confirmar_cotizacion": "Perfecto. ¿Me pasás tu nombre completo, cédula, celular y correo?",
                "crear_pedido": "Listo, tu pedido quedó creado. Te paso los datos para la transferencia.",
            }[llamada.name])

        self.prompts.append(system_blocks[-1])
        texto = str(ultimo.content).lower()
        if "quiero el plan premium" in texto:
            return self._tool("generar_cotizacion", items=[{"variant_id": self.variant_id, "quantity": 1}])
        if "confirmo" in texto:
            quote_id = self._quote_id(system_blocks)
            self.ids_usados.append(quote_id)
            return self._tool("confirmar_cotizacion", quote_id=quote_id)
        if "mis datos" in texto:
            quote_id = self._quote_id(system_blocks)
            self.ids_usados.append(quote_id)
            return self._tool("crear_pedido", quote_id=quote_id, payment_method="transferencia", customer_data=DATOS)
        return _Respuesta("end_turn", content="¿En qué más te ayudo?")


@pytest.fixture
async def packs_activos() -> None:
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager

    if mcp_host.get_tool("generar_cotizacion") is None:
        plugin_manager.scan()
        await plugin_manager.sync_registry()
        await plugin_manager.activate_all_configured()
        await plugin_manager.ensure_process_activation()


TURNOS = [
    "Hola, quiero el Plan Premium",
    "Sí, confirmo ese total",
    "Mis datos: Ana Gómez, cédula 1012345678, celular 3001234567, correo ana@example.com",
    "Gracias, ¿qué sigue?",
]


async def _compra(monkeypatch, sin_referencias: bool = False):  # type: ignore[no-untyped-def]
    from pack_commerce.models import Order, Quote
    from sqlalchemy import select

    from core.db.session import get_session
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    tid, variante = await _negocio()
    conv, _ = await _conversacion(tid)
    modelo = _Comprador(variante)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: modelo)
    if sin_referencias:
        async def ninguna(*_a, **_k):  # type: ignore[no-untyped-def]
            return []

        monkeypatch.setattr("core.orchestrator.loop.cargar_referencias", ninguna)

    respuestas = []
    for turno in TURNOS:
        async with tenant_scope(tid):
            respuestas.append(await process_conversation(conv, turno))

    async with tenant_scope(tid):
        async with get_session() as s:
            cotizaciones = (await s.execute(select(Quote).where(Quote.tenant_id == tid, Quote.conversation_id == conv))).scalars().all()
            pedidos = (await s.execute(select(Order).where(Order.tenant_id == tid, Order.conversation_id == conv))).scalars().all()
    return modelo, respuestas, cotizaciones, pedidos


async def test_compra_premium_en_varios_turnos_con_el_quote_id_del_prompt(monkeypatch, packs_activos) -> None:
    modelo, respuestas, cotizaciones, pedidos = await _compra(monkeypatch)

    assert all(r is not None and not r.escalated for r in respuestas), [r and r.text for r in respuestas]
    assert len(cotizaciones) == 1, "una sola cotización en toda la compra"
    quote_id = str(cotizaciones[0].id)
    assert modelo.ids_usados == [quote_id, quote_id], "confirmar y crear_pedido con el id real, sin reintentos"
    assert cotizaciones[0].status == "convertida"
    assert len(pedidos) == 1 and pedidos[0].quote_id == cotizaciones[0].id

    # El turno 1 no tiene referencias; el 2 y el 3, la cotización; el 4, solo el pedido.
    assert "Referencias vigentes" not in modelo.prompts[0]
    assert f"quote_id={quote_id}, estado=draft" in modelo.prompts[1]
    assert f"quote_id={quote_id}, estado=aceptada" in modelo.prompts[2]
    assert "- cotizacion:" not in modelo.prompts[3]
    assert f"numero={pedidos[0].public_order_number}" in modelo.prompts[3]


async def test_sin_referencias_el_error_con_quote_id_vigente_igual_cierra(monkeypatch, packs_activos) -> None:
    """Red de seguridad del paso 2: aunque el bloque falte, el modelo que adivina el id
    recibe `quote_id_vigente`, reintenta con él y no genera otra cotización."""
    modelo, respuestas, cotizaciones, pedidos = await _compra(monkeypatch, sin_referencias=True)

    assert all(r is not None and not r.escalated for r in respuestas)
    assert len(cotizaciones) == 1
    quote_id = str(cotizaciones[0].id)
    assert modelo.ids_usados == ["PLAN-PREMIUM", quote_id, "PLAN-PREMIUM", quote_id]
    assert len(pedidos) == 1 and cotizaciones[0].status == "convertida"
