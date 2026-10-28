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
- Entre la cotización vigente y el último pedido, el más nuevo es la compra en curso: un
  pedido de otra compra sale como `pedido_anterior`, sin total (incidente del 2026-10-10,
  19:34-19:49: OC-0001 del Basic, pagado, salía como "pedido" junto a la cotización del
  Premium y el modelo dijo "ya figura como pagado, así que no hace falta generar otro").
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
PRECIO_BASIC = 200000.0
DATOS = {"full_name": "Ana Gómez", "id_document": "1012345678", "phone": "3001234567", "email": "ana@example.com"}
_QUOTE_EN_PROMPT = re.compile(r"- cotizacion: quote_id=([0-9a-f-]{36})")


# ------------------------------------------------------------------ negocio y datos
async def _negocio(con_basic: bool = False) -> tuple[uuid.UUID, str]:
    """Negocio propio con pack-commerce y un plan Premium; devuelve (tenant, variant_id).

    Sin pack de suscripciones: comprar el Premium con el Basic pagado es un pedido nuevo,
    no un cambio de plan (`sub_cambiar_plan`). Con `con_basic`, también el Plan Basic."""
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
            if con_basic:
                basic = Product(tenant_id=tid, name="Plan Basic")
                s.add(basic)
                await s.flush()
                s.add(ProductVariant(
                    tenant_id=tid, product_id=basic.id, sku="PLAN-BASIC", price=PRECIO_BASIC, billing_period="mensual"
                ))
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

    assert await _lineas(tid, conv, cliente) == [
        f"- cotizacion: quote_id={propia}, estado=draft, total=600000.00, pedido=ninguno"
    ]


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


async def _pedido(
    tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID, *, status: str = "abierto", payment_status: str = "pendiente"
) -> tuple[str, str]:
    """Pedido de la conversación con el número que asigna el pack; devuelve (order_id, número)."""
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            seq = (await s.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})).scalar_one()
            order = Order(
                tenant_id=tid, conversation_id=conv, customer_id=cliente, status=status,
                payment_status=payment_status, total=PRECIO_BASIC, idempotency_key=f"refs:{uuid.uuid4().hex}",
                order_seq=seq, public_order_number=f"OC-{seq:04d}",
            )
            s.add(order)
            await s.flush()
            return str(order.id), order.public_order_number


async def test_pedido_pagado_de_otra_compra_sale_como_anterior_sin_total() -> None:
    """El caso del incidente: OC-0001 (Basic, pagado) y después la cotización del Premium."""
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    order_id, numero = await _pedido(tid, conv, cliente, payment_status="pagado")
    quote = await _cotizacion(tid, conv, cliente, variante)

    assert await _lineas(tid, conv, cliente) == [
        f"- cotizacion: quote_id={quote}, estado=draft, total=600000.00, pedido=ninguno",
        f"- pedido_anterior: order_id={order_id}, numero={numero}, estado=abierto, estado_pago=pagado",
    ]


async def test_pedido_pendiente_de_otra_compra_conserva_el_order_id() -> None:
    """Un pedido sin pagar sigue cobrable con `confirmar_pago_pedido` aunque el cliente
    cotice otra cosa: es anterior, pero lleva su `order_id`."""
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    order_id, _ = await _pedido(tid, conv, cliente)
    await _cotizacion(tid, conv, cliente, variante)

    lineas = await _lineas(tid, conv, cliente)
    assert lineas[1].startswith(f"- pedido_anterior: order_id={order_id},")
    assert "estado_pago=pendiente" in lineas[1] and "total=" not in lineas[1]


async def test_una_cotizacion_mas_vieja_que_el_pedido_no_se_expone() -> None:
    tid, variante = await _negocio()
    conv, cliente = await _conversacion(tid)
    await _cotizacion(tid, conv, cliente, variante)  # draft abandonado
    order_id, numero = await _pedido(tid, conv, cliente)

    assert await _lineas(tid, conv, cliente) == [
        f"- pedido: order_id={order_id}, numero={numero}, estado=abierto, estado_pago=pendiente, total=200000.00"
    ]


@pytest.mark.parametrize("status", ["cancelado", "entregado", "expirado"])
async def test_un_pedido_cerrado_es_anterior_aunque_no_haya_cotizacion(status: str) -> None:
    tid, _ = await _negocio()
    conv, cliente = await _conversacion(tid)
    order_id, numero = await _pedido(tid, conv, cliente, status=status, payment_status="pagado")

    assert await _lineas(tid, conv, cliente) == [
        f"- pedido_anterior: order_id={order_id}, numero={numero}, estado={status}, estado_pago=pagado"
    ]


@pytest.mark.parametrize("quote_id", ["PLAN-PREMIUM", "OC-0001", "Q-0001", str(uuid.uuid5(uuid.NAMESPACE_DNS, "cotizacion-inexistente"))])
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


# ------------------------------------------------------------------ journey: dos compras
_PEDIDO_PAGADO_EN_PROMPT = re.compile(r"^- pedido: .*estado_pago=pagado", re.MULTILINE)
INCIDENTE = "Tu pedido OC-0001 ya figura como pagado, así que no hace falta generar otro link."
SIN_PEDIDO = "El Premium todavía no tiene pedido. ¿Confirmás el total de la cotización para crearlo?"
CORREGIDA = "Me corrijo: el Premium todavía no tiene pedido ni pago. ¿Confirmás el total para crearlo?"


class _CompradorDosCompras(_Comprador):
    """Compra el Basic y, en la misma conversación, el Premium. Al preguntar cómo pagar,
    si el prompt muestra un `- pedido:` pagado responde como en el incidente (une ese pago
    con la compra actual); si no, sigue con la cotización. Ante el reintento del
    verificador (`[SISTEMA] …`) se corrige."""

    def __init__(self, basic_id: str, premium_id: str) -> None:
        super().__init__(premium_id)
        self.basic_id = basic_id
        self.reintentos = 0
        self.prompts_pago: list[str] = []

    async def generate(self, **kwargs: Any) -> _Respuesta:
        ultimo = kwargs["messages"][-1]
        if ultimo.role == "tool":
            resultado = json.loads(ultimo.content)
            if kwargs["messages"][-2].tool_calls[0].name == "generar_cotizacion" and "total" in resultado:
                total = "$" + f"{int(resultado['total']):,}".replace(",", ".")
                return _Respuesta("end_turn", content=f"Tu cotización da {total} al mes. ¿Confirmás ese total?")
            return await super().generate(**kwargs)
        texto = str(ultimo.content)
        if texto.startswith("[SISTEMA]"):
            self.reintentos += 1
            return _Respuesta("end_turn", content=CORREGIDA)
        if "quiero el plan basic" in texto.lower():
            return self._tool("generar_cotizacion", items=[{"variant_id": self.basic_id, "quantity": 1}])
        if "cómo lo pago" in texto.lower():
            bloque = kwargs["system_blocks"][-1]
            self.prompts_pago.append(bloque)
            return _Respuesta("end_turn", content=INCIDENTE if _PEDIDO_PAGADO_EN_PROMPT.search(bloque) else SIN_PEDIDO)
        return await super().generate(**kwargs)


COMPRA_BASIC = ["Hola, quiero el Plan Basic", "Sí, confirmo ese total", TURNOS[2]]
COMPRA_PREMIUM = ["Ahora quiero el Plan Premium", "¿Cómo lo pago?", "Sí, confirmo ese total", TURNOS[2]]


async def _dos_compras(monkeypatch, referencias_viejas: bool = False):  # type: ignore[no-untyped-def]
    """Basic comprado y pagado (el pago se marca como lo haría el webhook) y, en la misma
    conversación, la compra del Premium. Con `referencias_viejas`, el bloque se arma como
    antes del arreglo (el pedido anterior sale como `- pedido:`) para probar la red del
    verificador."""
    from pack_commerce.models import Order, ProductVariant, Quote
    from sqlalchemy import select, update

    from core.db.session import get_session
    from core.orchestrator import loop
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    tid, premium = await _negocio(con_basic=True)
    async with tenant_scope(tid):
        async with get_session() as s:
            basic = (await s.execute(select(ProductVariant.id).where(
                ProductVariant.tenant_id == tid, ProductVariant.sku == "PLAN-BASIC"
            ))).scalar_one()
    conv, _ = await _conversacion(tid)
    modelo = _CompradorDosCompras(str(basic), premium)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: modelo)
    if referencias_viejas:
        reales = loop.cargar_referencias

        async def como_antes(*args, **kwargs):  # type: ignore[no-untyped-def]
            return [linea.replace("- pedido_anterior:", "- pedido:") for linea in await reales(*args, **kwargs)]

        monkeypatch.setattr("core.orchestrator.loop.cargar_referencias", como_antes)

    respuestas = []
    for turno in COMPRA_BASIC:
        async with tenant_scope(tid):
            respuestas.append(await process_conversation(conv, turno))
    async with tenant_scope(tid):
        async with get_session() as s:
            await s.execute(update(Order).where(Order.tenant_id == tid, Order.conversation_id == conv)
                            .values(payment_status="pagado"))
    for turno in COMPRA_PREMIUM:
        async with tenant_scope(tid):
            respuestas.append(await process_conversation(conv, turno))

    async with tenant_scope(tid):
        async with get_session() as s:
            cotizaciones = (await s.execute(
                select(Quote).where(Quote.tenant_id == tid, Quote.conversation_id == conv).order_by(Quote.created_at)
            )).scalars().all()
            pedidos = (await s.execute(
                select(Order).where(Order.tenant_id == tid, Order.conversation_id == conv).order_by(Order.created_at)
            )).scalars().all()
    return modelo, respuestas, cotizaciones, pedidos


async def test_con_un_pedido_viejo_pagado_compra_otro_plan_en_la_misma_conversacion(monkeypatch, packs_activos) -> None:
    modelo, respuestas, cotizaciones, pedidos = await _dos_compras(monkeypatch)

    assert all(r is not None and not r.escalated for r in respuestas), [r and r.text for r in respuestas]
    viejo = pedidos[0]
    # Al preguntar cómo pagar el Premium, el pedido pagado del Basic es de otra compra.
    (bloque,) = modelo.prompts_pago
    assert "pedido=ninguno" in bloque
    assert f"- pedido_anterior: order_id={viejo.id}, numero={viejo.public_order_number}" in bloque
    assert "estado_pago=pagado" in bloque and "\n- pedido:" not in bloque
    assert respuestas[4].text == SIN_PEDIDO
    assert modelo.reintentos == 0

    # Termina con el pedido del Premium, pendiente, sobre su propia cotización.
    assert [c.status for c in cotizaciones] == ["convertida", "convertida"]
    assert len(pedidos) == 2
    nuevo = pedidos[1]
    assert nuevo.quote_id == cotizaciones[1].id and nuevo.public_order_number != viejo.public_order_number
    assert float(nuevo.total) == PRECIO and nuevo.payment_status == "pendiente"
    assert viejo.payment_status == "pagado"


async def test_si_el_modelo_une_el_pago_viejo_el_verificador_lo_frena(monkeypatch, packs_activos) -> None:
    """Red del verificador: con el bloque como antes del arreglo, el modelo repite la frase
    del incidente sin consultar el estado; `pago_registrado` la rechaza y el cliente recibe
    la corrección, nunca "ya figura como pagado"."""
    modelo, respuestas, _cotizaciones, pedidos = await _dos_compras(monkeypatch, referencias_viejas=True)

    assert modelo.reintentos == 1
    assert respuestas[4] is not None and respuestas[4].text == CORREGIDA
    assert "figura como pagado" not in " ".join(r.text for r in respuestas if r is not None)
    assert len(pedidos) == 2 and pedidos[1].payment_status == "pendiente"
