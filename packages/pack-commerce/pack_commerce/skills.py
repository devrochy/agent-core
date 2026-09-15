"""Skills del CRM v2: catálogo, ventas (cotización -> pedido) y leads.

Adaptación de los dominios de agent-sale (commerce + catalog) al contrato de
agent-core. Reglas de oro:
- El LLM NUNCA inventa precios/stock: consultar_inventario es la única fuente.
- Datos de pago/transferencia van por _safe_append (nunca por el texto del LLM).
- Toda escritura es idempotente con context.idempotency_key.
- Montos altos escalan a humano (monto_alto_threshold del config).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from core.db.session import get_owned, get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.memory import entity as entity_memory
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import (
    CategoryComplement,
    Inventory,
    Lead,
    Order,
    OrderItem,
    OrderItemBatch,
    Product,
    ProductVariant,
    Promotion,
    PromotionRedemption,
    Quote,
    QuoteItem,
)

CUSTOMER_SEGMENTS = ("nuevo", "ocasional", "frecuente", "fiel", "inactivo")

ORDER_STATUS_ACTIVE = ("abierto", "despachado")

PAYMENT_METHODS = ("efectivo_contraentrega", "transferencia", "pago_en_linea")
DELIVERY_METHODS = ("domicilio", "recoger_en_tienda")


# ------------------------------------------------------------------ helpers
def _float(value: Any) -> float:
    return float(value or 0)


def _money(value: Any, symbol: str = "$") -> str:
    return f"{symbol}{_float(value):,.0f}"


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_phone(phone: str) -> str:
    return re.sub(r"[^\d]", "", phone).lstrip("0")


def _parse_uuid(value: str) -> uuid.UUID | None:
    """Parsea un ID que el LLM pasó como argumento. `None` si no es un UUID válido.

    Encontrado en vivo (tenant yopoldo, 2026-09-11): el LLM inventó
    `variant_id="pollo_broaster"` en vez de usar el UUID real que
    `consultar_inventario` acababa de devolver. Sin validar esto ANTES de
    consultar, el string inválido llegaba directo a Postgres
    (`asyncpg.exceptions.DataError`), una excepción no manejada que el core
    interpreta como una falla real del plugin y lo desactiva -- GLOBALMENTE,
    para todos los tenants (`PluginRecord.status` no es por-tenant). Un
    argumento inválido de un LLM (que va a pasar periódicamente) no debería
    poder tumbar el pack entero: se valida acá y se devuelve un error que el
    LLM puede corregir en su propio turno, en vez de una excepción."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _plugin_config() -> Any:  # type: ignore[no-untyped-def]
    """Config del plugin con fallback a None (robusto en pruebas/activación parcial)."""
    try:
        from core.plugin_manager import plugin_manager

        return await plugin_manager.load_config("pack_commerce")
    except Exception:
        return None


async def classify_customer(customer_id) -> str:  # type: ignore[no-untyped-def]
    """Clasifica al cliente en 5 segmentos según sus pedidos (ag-sale ADR-036).

    Derivado en tiempo real desde crm_orders; los umbrales vienen del config.
    """
    config = await _plugin_config()

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Order.created_at)
                .where(
                    Order.customer_id == customer_id,
                    Order.status != "expirado",
                    Order.tenant_id == get_current_tenant_id(),
                )
                .order_by(Order.created_at)
            )
        ).scalars().all()

    count = len(rows)
    if count == 0:
        return "nuevo"

    freq_min = getattr(config, "customer_frecuente_min_pedidos", 2) or 2
    fiel_min = getattr(config, "customer_fiel_min_pedidos", 5) or 5
    intervalo_max = getattr(config, "customer_frecuente_intervalo_max_dias", 45) or 45
    inactivo_dias = getattr(config, "customer_inactivo_dias_sin_comprar", 120) or 120

    segment = "ocasional"
    if count >= fiel_min:
        segment = "fiel"
    elif count >= freq_min:
        intervalos = [(rows[i] - rows[i - 1]).days for i in range(1, len(rows))]
        if intervalos and max(intervalos) <= intervalo_max:
            segment = "frecuente"

    dias_sin_comprar = (_now() - rows[-1]).days
    if dias_sin_comprar > inactivo_dias:
        segment = "inactivo"
    return segment


async def _active_customer_order(customer_id) -> Order | None:  # type: ignore[no-untyped-def]
    async with get_session() as session:
        result = await session.execute(
            select(Order)
            .where(
                Order.customer_id == customer_id,
                Order.status.in_(ORDER_STATUS_ACTIVE),
                Order.tenant_id == get_current_tenant_id(),
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _transfer_safe_append() -> str:
    """Arma el bloque de datos de transferencia (nunca lo escribe el LLM)."""
    config = await _plugin_config()
    accounts = getattr(config, "transfer_accounts", []) or []
    if not accounts:
        return ""
    lines = ["💳 Datos para tu transferencia:"]
    for acc in accounts:
        entity = acc.get("entity", "")
        account_type = acc.get("accountType", "")
        number = acc.get("accountNumber", "")
        holder = acc.get("holderName", "")
        doc = acc.get("holderDocument", "")
        line = f"• {entity}"
        if account_type:
            line += f" ({account_type})"
        if number:
            line += f" N° {number}"
        if holder:
            line += f" a nombre de {holder}"
        if doc:
            line += f" CC {doc}"
        lines.append(line)
    return "\n".join(lines)


async def _get_variant_with_stock(variant_id_or_sku) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """Busca una variante por `id` real o, si no es un UUID, por `sku` exacto.

    El cliente suele referirse a un producto por su SKU ("A-001"), no por el
    UUID interno -- exigirle al LLM que siempre tenga el UUID a mano (vía
    `consultar_inventario` primero) lo empuja a consultar inventario incluso
    cuando el cliente ya fue explícito, así que las tools que llaman a esta
    función aceptan cualquiera de los dos.
    """
    value = str(variant_id_or_sku or "").strip()
    if not value:
        return None
    tenant_id = get_current_tenant_id()
    parsed = _parse_uuid(value)
    condition = ProductVariant.id == parsed if parsed is not None else ProductVariant.sku == value
    async with get_session() as session:
        result = await session.execute(
            select(ProductVariant, Product, Inventory)
            .join(Product, Product.id == ProductVariant.product_id)
            .outerjoin(Inventory, Inventory.variant_id == ProductVariant.id)
            .where(condition, ProductVariant.tenant_id == tenant_id)
        )
        row = result.first()
    if row is None:
        return None
    variant, product, inventory = row
    return {
        "variant": variant,
        "product": product,
        "stock": inventory.stock_quantity if inventory else 0,
        "price": _float(variant.price),
    }


def _validate_delivery_data(data: dict[str, Any]) -> tuple[bool, list[str]]:
    required = ("full_name", "id_document", "address", "municipality", "city")
    missing = [f for f in required if not str(data.get(f, "")).strip()]
    return (not missing, missing)


# ------------------------------------------------------------------ LEADS
class LeadSkill(BaseSkill):
    name = "lead_skill"
    description = "Captura y calificación de leads (prospectos de venta)"
    keywords: list[str] = []  # noqa: RUF012  (siempre activa)

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="crear_lead",
                description=(
                    "Registra un nuevo prospecto (lead) en el CRM. Úsala cuando el cliente "
                    "muestre interés y ya tengas al menos su nombre. NO la uses dos veces "
                    "para la misma persona: si ya existe, usa actualizar_lead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nombre completo del prospecto"},
                        "email": {"type": "string", "description": "Email si lo dio"},
                        "phone": {"type": "string", "description": "Teléfono si lo dio"},
                        "interest": {"type": "string", "description": "Qué producto/servicio le interesa"},
                    },
                    "required": ["name", "interest"],
                },
                handler=self.crear_lead,
            ),
            MCPTool(
                name="calificar_lead",
                description=(
                    "Asigna una puntuación 1-10 al lead según presupuesto, urgencia y poder de "
                    "decisión. Úsala después de hacer al menos 2 preguntas de calificación."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "score": {"type": "integer", "description": "Puntuación 1-10"},
                        "notes": {"type": "string", "description": "Justificación breve"},
                    },
                    "required": ["score"],
                },
                handler=self.calificar_lead,
            ),
            MCPTool(
                name="actualizar_lead",
                description="Actualiza datos del lead existente de esta conversación (nombre, email, teléfono, interés, estado).",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nombre completo del prospecto"},
                        "email": {"type": "string"},
                        "phone": {"type": "string", "description": "Teléfono si lo dio"},
                        "interest": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["nuevo", "calificado", "contactado", "convertido", "perdido"],
                        },
                    },
                },
                handler=self.actualizar_lead,
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def crear_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            tenant_id = get_current_tenant_id()
            existing = (
                await session.execute(
                    select(Lead).where(
                        Lead.idempotency_key == context.idempotency_key,
                        Lead.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"lead_id": str(existing.id), "status": "duplicate"}

            by_customer = (
                await session.execute(
                    select(Lead).where(
                        Lead.customer_id == context.customer_id, Lead.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if by_customer is not None:
                return {
                    "lead_id": str(by_customer.id),
                    "status": "ya_existe",
                    "hint": "Este cliente ya es un lead. Usa actualizar_lead.",
                }

            lead = Lead(
                customer_id=context.customer_id,
                conversation_id=context.conversation_id,
                name=arguments.get("name"),
                email=arguments.get("email"),
                phone=arguments.get("phone"),
                interest=arguments.get("interest", ""),
                idempotency_key=context.idempotency_key,
            )
            session.add(lead)
            await session.flush()
            lead_id = str(lead.id)

        # Entity Memory (Capa 2): el core recuerda esto en TODAS las conversaciones futuras
        await entity_memory.upsert_attributes(
            context.customer_id,
            {"name": arguments.get("name"), "email": arguments.get("email"), "crm_lead_id": lead_id},
        )
        return {"lead_id": lead_id, "status": "created"}

    async def calificar_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        try:
            score = max(1, min(10, int(arguments.get("score", 0))))
        except (TypeError, ValueError):
            return {"error": "score inválido: debe ser un número entre 1 y 10."}
        async with get_session() as session:
            lead = (
                await session.execute(
                    select(Lead).where(
                        Lead.customer_id == context.customer_id,
                        Lead.tenant_id == get_current_tenant_id(),
                    )
                )
            ).scalar_one_or_none()
            if lead is None:
                return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
            lead.score = score
            lead.status = "calificado"
            lead.last_activity_at = _now()
        return {"lead_id": str(lead.id), "score": score, "status": "calificado"}

    async def actualizar_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            lead = (
                await session.execute(
                    select(Lead).where(
                        Lead.customer_id == context.customer_id,
                        Lead.tenant_id == get_current_tenant_id(),
                    )
                )
            ).scalar_one_or_none()
            if lead is None:
                return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
            if arguments.get("name"):
                lead.name = str(arguments["name"])
            if arguments.get("email"):
                lead.email = str(arguments["email"])
            if arguments.get("phone"):
                lead.phone = str(arguments["phone"])
            if arguments.get("interest"):
                lead.interest = str(arguments["interest"])
            if arguments.get("status"):
                lead.status = str(arguments["status"])
            lead.last_activity_at = _now()
        if arguments.get("email"):
            await entity_memory.upsert_attributes(context.customer_id, {"email": arguments["email"]})
        return {"lead_id": str(lead.id), "status": "actualizado"}


# ------------------------------------------------------------------ CATÁLOGO
class CatalogSkill(BaseSkill):
    name = "catalog_skill"
    description = "Catálogo, precios y disponibilidad de productos (el menú del negocio)"
    keywords: list[str] = []  # noqa: RUF012  (siempre activa)

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="consultar_inventario",
                description=(
                    "Consulta precios y disponibilidad reales de los productos del menú. "
                    "Úsala SIEMPRE antes de afirmar cualquier precio, stock o disponibilidad. "
                    "Nunca inventes precios: si el cliente pregunta por algo, consulta aquí primero."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Búsqueda libre, ej: 'hamburguesa'"},
                        "sku": {"type": "string", "description": "Código exacto del producto"},
                    },
                },
                handler=self.consultar_inventario,
            ),
            MCPTool(
                name="recomendar_producto",
                description=(
                    "Sugiere productos complementarios o relacionados (venta cruzada), "
                    "ej. un complemento para el producto que eligió. Úsala después de que el cliente "
                    "muestre interés en un producto concreto."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "description": "ID del producto de referencia"},
                        "context": {"type": "string", "description": "Contexto de la preferencia del cliente"},
                    },
                },
                handler=self.recomendar_producto,
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def consultar_inventario(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        sku = str(arguments.get("sku", "")).strip()

        async with get_session() as session:
            stmt = (
                select(ProductVariant, Product, Inventory)
                .join(Product, Product.id == ProductVariant.product_id)
                .outerjoin(Inventory, Inventory.variant_id == ProductVariant.id)
                .where(
                    ProductVariant.active.is_(True),
                    ProductVariant.tenant_id == get_current_tenant_id(),
                )
            )
            if sku:
                stmt = stmt.where(ProductVariant.sku == sku)
            elif query:
                like = f"%{query}%"
                stmt = stmt.where(
                    Product.name.ilike(like) | ProductVariant.sku.ilike(like)
                )
            stmt = stmt.order_by(Product.name).limit(15)
            rows = (await session.execute(stmt)).all()

        matches = []
        for variant, product, inventory in rows:
            matches.append(
                {
                    "product_id": str(product.id),
                    "variant_id": str(variant.id),
                    "sku": variant.sku,
                    "name": product.name,
                    "attributes": variant.attributes or {},
                    "price": _float(variant.price),
                    "price_label": _money(variant.price),
                    "disponible": bool(inventory and inventory.stock_quantity > 0),
                    "stock": inventory.stock_quantity if inventory else 0,
                    "description": product.description or "",
                }
            )
        if not matches:
            return {
                "matches": [],
                "message": "No encontré productos para esa búsqueda. Pide más detalles o sugiere ver el menú completo.",
            }
        return {"matches": matches}

    async def recomendar_producto(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        product_id = str(arguments.get("product_id", "")).strip()
        async with get_session() as session:
            product = None
            if product_id and _parse_uuid(product_id) is not None:
                product = await get_owned(session, Product, product_id, get_current_tenant_id())
            if product is None or product.category_id is None:
                return {"recommendations": []}

            complements = (
                await session.execute(
                    select(CategoryComplement.complementary_category_id).where(
                        CategoryComplement.category_id == product.category_id,
                        CategoryComplement.tenant_id == get_current_tenant_id(),
                    )
                )
            ).scalars().all()

            if not complements:
                return {"recommendations": []}

            rows = (
                await session.execute(
                    select(ProductVariant, Product, Inventory)
                    .join(Product, Product.id == ProductVariant.product_id)
                    .outerjoin(Inventory, Inventory.variant_id == ProductVariant.id)
                    .where(
                        Product.category_id.in_(complements),
                        Product.id != product.id,
                        ProductVariant.active.is_(True),
                        Inventory.stock_quantity > 0,
                        ProductVariant.tenant_id == get_current_tenant_id(),
                    )
                    .order_by(Product.name)
                    .limit(5)
                )
            ).all()

        recommendations = [
            {
                "product_id": str(p.id),
                "variant_id": str(v.id),
                "name": p.name,
                "price": _float(v.price),
                "price_label": _money(v.price),
                "reason": "combina bien con lo que estás pidiendo",
            }
            for v, p, inv in rows
        ]
        return {"recommendations": recommendations}


# ------------------------------------------------------------------ VENTAS
class OrderSkill(BaseSkill):
    name = "order_skill"
    description = "Cotización, promociones, pedidos y consulta de estado de compra"
    keywords: list[str] = [  # noqa: RUF012
        "pedir", "pedido", "comprar", "cotizar", "cotización", "cotizacion",
        "domicilio", "entrega", "llevar", "encargar", "orden", "quitar", "agregar",
        "estado de mi pedido", "dónde va", "cuánto vale", "cuánto cuesta", "cuánto es",
        "menú", "menu", "carta", "deseo", "quiero", "me gustaría",
    ]

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="generar_cotizacion",
                description=(
                    "Crea una cotización con los productos y cantidades que el cliente pidió. "
                    "Vuelve a validar precio y stock reales (nunca confíes en lo que dice el LLM). "
                    "Úsala cuando el cliente confirme qué quiere, aunque sea un menú completo. "
                    "Si el cliente ya te dio un SKU (ej. \"A-001\") no hace falta llamar "
                    "consultar_inventario antes: pasá el SKU directo en variant_id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "description": "Lista de {variant_id, quantity}",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "variant_id": {
                                        "type": "string",
                                        "description": "ID real de la variante, o su SKU si eso es lo que te dio el cliente",
                                    },
                                    "quantity": {"type": "integer"},
                                },
                                "required": ["variant_id", "quantity"],
                            },
                        },
                    },
                    "required": ["items"],
                },
                handler=self.generar_cotizacion,
            ),
            MCPTool(
                name="aplicar_promocion",
                description=(
                    "Evalúa las promociones activas contra una cotización y aplica automáticamente "
                    "la de mayor beneficio para el cliente (nunca se combinan promociones). "
                    "Úsala apenas se genera una cotización y cuando el cliente pregunte por descuentos."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "quote_id": {"type": "string", "description": "ID de la cotización"},
                    },
                    "required": ["quote_id"],
                },
                handler=self.aplicar_promocion,
            ),
            MCPTool(
                name="confirmar_cotizacion",
                description=(
                    "Marca una cotización como aceptada por el cliente. Llamala SOLO cuando el "
                    "cliente ya vio el total final (con la promoción aplicada si corresponde) y "
                    "dijo que sí explícitamente. Es el paso OBLIGATORIO antes de crear_pedido -- "
                    "esa tool falla si la cotización no pasó por acá."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "quote_id": {"type": "string", "description": "ID de la cotización"},
                    },
                    "required": ["quote_id"],
                },
                handler=self.confirmar_cotizacion,
            ),
            MCPTool(
                name="crear_pedido",
                description=(
                    "Convierte una cotización aceptada por el cliente en un pedido confirmado. "
                    "Úsala SOLO después de haber llamado confirmar_cotizacion en esta conversación "
                    "y de que el cliente haya acordado método de pago y de entrega."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "quote_id": {"type": "string"},
                        "payment_method": {
                            "type": "string",
                            "enum": ["efectivo_contraentrega", "transferencia", "pago_en_linea"],
                            "description": (
                                "'pago_en_linea' SOLO si el pack de pagos está activo para este "
                                "tenant -- llamá luego generar_link_pago con la referencia del pedido."
                            ),
                        },
                        "delivery_method": {
                            "type": "string",
                            "enum": ["domicilio", "recoger_en_tienda"],
                        },
                        "customer_data": {
                            "type": "object",
                            "description": "Datos de quien recibe el pedido",
                            "properties": {
                                "full_name": {"type": "string"},
                                "id_document": {"type": "string"},
                                "address": {"type": "string"},
                                "municipality": {"type": "string"},
                                "city": {"type": "string"},
                                "phone": {"type": "string"},
                            },
                            "required": ["full_name", "id_document", "address", "municipality", "city"],
                        },
                    },
                    "required": ["quote_id", "payment_method", "delivery_method", "customer_data"],
                },
                handler=self.crear_pedido,
            ),
            MCPTool(
                name="agregar_item_pedido",
                description=(
                    "Suma productos a un pedido ya confirmado en la misma conversación, "
                    "mientras siga abierto (aún no entregado). No crea pedido ni cotización nueva. "
                    "Si el cliente ya te dio un SKU (ej. \"A-001\") no hace falta llamar "
                    "consultar_inventario antes: pasá el SKU directo en variant_id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "variant_id": {
                                        "type": "string",
                                        "description": "ID real de la variante, o su SKU si eso es lo que te dio el cliente",
                                    },
                                    "quantity": {"type": "integer"},
                                },
                                "required": ["variant_id", "quantity"],
                            },
                        },
                    },
                    "required": ["order_id", "items"],
                },
                handler=self.agregar_item_pedido,
            ),
            MCPTool(
                name="consultar_estado_pedido",
                description=(
                    "Responde el estado real de un pedido a partir de su número público (ej. 'OC-0001'). "
                    "Úsala cuando el cliente pregunte cómo va su pedido. Nunca inventes el estado."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "public_order_number": {"type": "string", "description": "Número del pedido, ej. OC-0001"},
                    },
                    "required": ["public_order_number"],
                },
                handler=self.consultar_estado_pedido,
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def generar_cotizacion(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        items = arguments.get("items") or []
        if not items or not isinstance(items, list):
            return {"error": "Necesito al menos un producto para cotizar."}
        config = await _plugin_config()

        rows: list[dict[str, Any]] = []
        subtotal = 0.0
        for item in items:
            if not isinstance(item, dict):
                return {"error": "Cada producto de la cotización debe traer variant_id y quantity."}
            variant_ref = str(item.get("variant_id", ""))
            try:
                quantity = int(item.get("quantity", 0))
            except (TypeError, ValueError):
                return {"error": "Cantidad inválida para un producto de la cotización."}
            if quantity <= 0:
                return {"error": "Las cantidades deben ser números enteros positivos."}
            data = await _get_variant_with_stock(variant_ref)
            if data is None or not data["variant"].active:
                return {"error": "Uno de los productos solicitados no está disponible."}
            if data["stock"] < quantity:
                return {
                    "error": f"No hay stock suficiente de {data['product'].name} "
                             f"(disponible: {data['stock']})."
                }
            line_total = data["price"] * quantity
            subtotal += line_total
            rows.append(
                {
                    "variant_id": str(data["variant"].id),
                    "product_name": data["product"].name,
                    "attributes": data["variant"].attributes or {},
                    "unit_price": data["price"],
                    "quantity": quantity,
                    "line_total": line_total,
                }
            )

        async with get_session() as session:
            quote = Quote(
                conversation_id=context.conversation_id,
                customer_id=context.customer_id,
                subtotal=round(subtotal, 2),
                discount=0,
                total=round(subtotal, 2),
                status="draft",
            )
            session.add(quote)
            await session.flush()
            for row in rows:
                session.add(
                    QuoteItem(
                        quote_id=quote.id,
                        variant_id=row["variant_id"],
                        quantity=row["quantity"],
                        unit_price=row["unit_price"],
                    )
                )
            quote_id = str(quote.id)

        return {
            "quote_id": quote_id,
            "status": "draft",
            "items": [
                {
                    "product": r["product_name"],
                    "attributes": r["attributes"],
                    "quantity": r["quantity"],
                    "unit_price": r["unit_price"],
                    "line_total": r["line_total"],
                }
                for r in rows
            ],
            "subtotal": round(subtotal, 2),
            "total": round(subtotal, 2),
            "moneda": getattr(config, "currency_code", "COP") or "COP",
        }

    async def aplicar_promocion(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        quote_id = str(arguments.get("quote_id", ""))
        if _parse_uuid(quote_id) is None:
            return {"error": "Cotización no encontrada."}
        async with get_session() as session:
            tenant_id = get_current_tenant_id()
            quote = await get_owned(session, Quote, quote_id, tenant_id)
            if quote is None:
                return {"error": "Cotización no encontrada."}
            items = (
                await session.execute(
                    select(QuoteItem).where(
                        QuoteItem.quote_id == quote.id, QuoteItem.tenant_id == tenant_id
                    )
                )
            ).scalars().all()

            variants = {
                v.id: (v, p, inv)
                for v, p, inv in (
                    await session.execute(
                        select(ProductVariant, Product, Inventory)
                        .join(Product, Product.id == ProductVariant.product_id)
                        .outerjoin(Inventory, Inventory.variant_id == ProductVariant.id)
                        .where(
                            ProductVariant.id.in_([i.variant_id for i in items]),
                            ProductVariant.tenant_id == tenant_id,
                        )
                    )
                ).all()
            }
            promotions = (
                await session.execute(
                    select(Promotion).where(
                        Promotion.active.is_(True),
                        (Promotion.valid_from.is_(None)) | (Promotion.valid_from <= _now()),
                        (Promotion.valid_to.is_(None)) | (Promotion.valid_to >= _now()),
                        Promotion.tenant_id == tenant_id,
                    )
                )
            ).scalars().all()

        if not promotions:
            return {
                "quote_id": quote_id,
                "applied": False,
                "message": "No hay promociones vigentes aplicables en este momento.",
            }

        segment = await classify_customer(quote.customer_id)
        line_details = []
        for item in items:
            pv, p, _inv = variants.get(item.variant_id, (None, None, None))
            if pv is None:
                continue
            line_details.append(
                {
                    "product": p,
                    "variant": pv,
                    "category_id": p.category_id,
                    "quantity": item.quantity,
                    "subtotal": _float(item.unit_price) * item.quantity,
                }
            )

        best: tuple[float, Promotion] | None = None  # (beneficio, promo)
        for promo in promotions:
            rules = promo.rules or {}
            kind = rules.get("kind", promo.type)
            if promo.eligible_segments and segment not in promo.eligible_segments:
                continue
            if not all(self._line_matches_promo(line, promo) for line in line_details):
                continue

            if kind == "temporada" or kind == "campaña":
                discount_pct = float(rules.get("discount_pct", 0))
                if (
                    promo.type == "campaña"
                    and rules.get("once_per_customer")
                    and await self._campaign_redeemed(promo.id, quote.customer_id)
                ):
                    continue
                benefit = _float(quote.subtotal) * discount_pct / 100
            elif kind == "volumen":
                total_qty = sum(line["quantity"] for line in line_details)
                benefit = self._volume_benefit(rules, total_qty, line_details, _float(quote.subtotal))
            else:
                benefit = 0

            if benefit > 0 and (best is None or benefit > best[0]):
                best = (benefit, promo)

        if best is None or best[0] <= 0:
            return {
                "quote_id": quote_id,
                "applied": False,
                "subtotal": _float(quote.subtotal),
                "total": _float(quote.total),
                "segmento": segment,
                "message": "No aplicó ninguna promoción.",
            }

        benefit, promo = best
        new_total = max(_float(quote.subtotal) - benefit, 0)
        async with get_session() as session:
            quote = await get_owned(session, Quote, quote_id, get_current_tenant_id())
            quote.discount = benefit
            quote.total = new_total
            quote.applied_promotion_id = promo.id
            quote.status = "draft"

        return {
            "quote_id": quote_id,
            "applied": True,
            "promotion_id": str(promo.id),
            "promotion_type": promo.type,
            "label": (promo.rules or {}).get("label", ""),
            "discount": round(benefit, 2),
            "subtotal": _float(quote.subtotal),
            "total": round(new_total, 2),
            "segmento": segment,
        }

    async def confirmar_cotizacion(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Transiciona `Quote.status` a "aceptada" -- el gate determinístico que
        faltaba (ver `crear_pedido`, que ahora lo exige antes de crear el
        pedido). Incidente real (tenant `yopoldo`, 2026-09-12): el modelo
        armaba bien la cotización (`generar_cotizacion` + `aplicar_promocion`
        reales) pero afirmaba haber cerrado el pedido en texto sin haber
        llamado `crear_pedido` -- `crm_completion_check` lo detectaba DESPUÉS
        de redactado, nunca lo prevenía. Comparado con `agent-sale`
        (`loop.ts::FORZAR_SIGUIENTE_TOOL`/`esTurnoSilencioso`), que hace esto
        imposible por diseño (tool_choice forzado + corte de turno): acá se
        resuelve más simple, con una precondición de datos en la propia tool
        (`Quote.status` ya declaraba "aceptada" en el schema, `models.py:186`,
        pero ningún código lo escribía jamás)."""
        quote_id = str(arguments.get("quote_id", ""))
        if _parse_uuid(quote_id) is None:
            return {"error": "Cotización no encontrada."}
        async with get_session() as session:
            # Defensa en profundidad además de RLS (mismo criterio que
            # calificar_lead/actualizar_lead, auditor-multitenant 2026-09-12):
            # filtrar tenant_id explícito en vez de session.get() a secas.
            quote = (
                await session.execute(
                    select(Quote).where(
                        Quote.id == quote_id, Quote.tenant_id == get_current_tenant_id()
                    )
                )
            ).scalar_one_or_none()
            if quote is None:
                return {"error": "Cotización no encontrada."}
            if quote.status == "convertida":
                return {"error": "Esta cotización ya se convirtió en un pedido."}
            if quote.status == "expirada":
                return {"error": "Esta cotización expiró. Generá una nueva con generar_cotizacion."}
            quote.status = "aceptada"
            total = _float(quote.total)

        return {"quote_id": quote_id, "status": "aceptada", "total": round(total, 2)}

    @staticmethod
    def _line_matches_promo(line: dict[str, Any], promo: Promotion) -> bool:
        return not (
            (promo.category_id and line.get("category_id") != promo.category_id)
            or (promo.product_id and line["product"].id != promo.product_id)
            or (promo.variant_id and line["variant"].id != promo.variant_id)
        )

    @staticmethod
    def _volume_benefit(rules: dict, total_qty: int, lines: list[dict], subtotal: float) -> float:
        tiers = rules.get("tiers") or []
        best = 0.0
        for tier in tiers:
            lo = int(tier.get("min", 0))
            hi = int(tier.get("max", 0)) or None
            if total_qty < lo or (hi is not None and total_qty > hi):
                continue
            if tier.get("discount_pct") is not None:
                best = max(best, subtotal * float(tier["discount_pct"]) / 100)
            elif tier.get("free_item_sku"):
                # aproximación: el producto gratis cuesta el precio de su variante
                for line in lines:
                    if line["variant"].sku == tier["free_item_sku"]:
                        best = max(best, float(line["variant"].price))
                        break
        return best

    async def _campaign_redeemed(self, promotion_id, customer_id) -> bool:  # type: ignore[no-untyped-def]
        async with get_session() as session:
            result = await session.execute(
                select(PromotionRedemption.id).where(
                    PromotionRedemption.promotion_id == promotion_id,
                    PromotionRedemption.customer_id == customer_id,
                    PromotionRedemption.tenant_id == get_current_tenant_id(),
                ).limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def crear_pedido(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        quote_id = str(arguments.get("quote_id", ""))
        payment_method = str(arguments.get("payment_method", ""))
        delivery_method = str(arguments.get("delivery_method", ""))
        customer_data = arguments.get("customer_data") or {}
        if not isinstance(customer_data, dict):
            return {
                "error": "Faltan datos de entrega",
                "missing_fields": ["full_name", "id_document", "address", "municipality", "city"],
                "hint": "Pide al cliente esos datos antes de confirmar el pedido.",
            }
        config = await _plugin_config()

        if payment_method not in PAYMENT_METHODS:
            return {
                "error": "Método de pago no válido. Usa 'efectivo_contraentrega', 'transferencia' o 'pago_en_linea'."
            }
        if delivery_method not in DELIVERY_METHODS:
            return {"error": "Método de entrega no válido. Usa 'domicilio' o 'recoger_en_tienda'."}
        if _parse_uuid(quote_id) is None:
            return {"error": "Cotización no encontrada. Genera una con generar_cotizacion."}

        async with get_session() as session:
            # Defensa en profundidad además de RLS (auditor-multitenant 2026-09-12).
            quote = (
                await session.execute(
                    select(Quote).where(
                        Quote.id == quote_id, Quote.tenant_id == get_current_tenant_id()
                    )
                )
            ).scalar_one_or_none()
            if quote is None:
                return {"error": "Cotización no encontrada. Genera una con generar_cotizacion."}
            existing_order = (
                await session.execute(
                    select(Order).where(
                        Order.quote_id == quote.id, Order.tenant_id == get_current_tenant_id()
                    )
                )
            ).scalar_one_or_none()
            if existing_order is not None:
                return {
                    "order_id": str(existing_order.id),
                    "status": "duplicate",
                    "public_order_number": existing_order.public_order_number,
                }
            if quote.status != "aceptada":
                # Gate determinístico (incidente real 2026-09-12, tenant yopoldo):
                # sin esto, el modelo podía "creer" que el cliente ya confirmó y
                # alucinar el cierre del pedido en texto sin llamar esta tool --
                # ahora ni siquiera puede completarla sin haber pasado antes por
                # confirmar_cotizacion, sin importar qué diga el texto del turno.
                return {
                    "error": "La cotización todavía no fue confirmada por el cliente.",
                    "hint": "Llamá confirmar_cotizacion primero, después de que el "
                    "cliente haya dicho que sí al total final.",
                }
            items = (
                await session.execute(
                    select(QuoteItem).where(
                        QuoteItem.quote_id == quote.id,
                        QuoteItem.tenant_id == get_current_tenant_id(),
                    )
                )
            ).scalars().all()
            if not items:
                return {"error": "La cotización no tiene productos."}

            threshold = getattr(config, "monto_alto_threshold", 1_000_000) or 1_000_000
            if _float(quote.total) > threshold:
                return {
                    "error": "El total supera el monto máximo permitido y requiere aprobación humana.",
                    "escalate": True,
                }

            min_online = getattr(config, "pago_en_linea_monto_minimo", 150_000) or 150_000
            if payment_method == "pago_en_linea" and _float(quote.total) < min_online:
                # Incidente real (tenant yopoldo, 2026-09-12): un pedido de
                # $54.000 con pago_en_linea nunca iba a poder completarse (el
                # mínimo real de la pasarela es $150.000, MIN_AMOUNT_COP en
                # pack_payments/providers/wompi.py -- no importable acá,
                # invariante 5). Antes esto solo se descubría (si el modelo
                # llegaba a llamarla) en generar_link_pago, ya tarde para
                # redirigir la conversación con naturalidad; ahora es
                # determinístico en el primer punto posible.
                #
                # SEGUNDO incidente real, mismo día: la primera versión de este
                # error incluía el monto exacto en texto libre ("...menores a
                # $150.000"). El modelo lo repetía en su respuesta, y
                # `_amount_guardrail` (module.py) lo bloqueaba por "monto no
                # verificado" -- ese guardrail solo reconoce montos que salen
                # de una clave de MONEY_KEYS en el resultado de una tool, no
                # números embebidos en un string de error. En vez de enseñarle
                # al guardrail a reconocer este caso puntual, se saca el monto
                # del texto libre: el modelo no necesita citarlo para cumplir
                # la regla del playbook ("ofrecé otro método").
                return {
                    "error": "El pago en línea no está disponible para este pedido.",
                    "hint": "Ofrecele transferencia o contraentrega en su lugar.",
                }

        complete, missing = _validate_delivery_data(customer_data)
        if not complete:
            return {
                "error": "Faltan datos de entrega",
                "missing_fields": missing,
                "hint": "Pide al cliente esos datos antes de confirmar el pedido.",
            }

        # Guardar datos de entrega en la memoria de entidad del core (Capa 2)
        await entity_memory.upsert_attributes(
            context.customer_id,
            {
                "full_name": customer_data.get("full_name"),
                "id_document": customer_data.get("id_document"),
                "address": customer_data.get("address"),
                "municipality": customer_data.get("municipality"),
                "city": customer_data.get("city"),
                "contact_phone": customer_data.get("phone"),
            },
        )

        async with get_session() as session:
            quote = (
                await session.execute(
                    select(Quote).where(
                        Quote.id == quote_id, Quote.tenant_id == get_current_tenant_id()
                    )
                )
            ).scalar_one_or_none()
            # Numeración por tenant (ADR-0018): contador atómico por tenant.
            order_seq = (
                await session.execute(
                    text("select pack_commerce_next_order_seq(:t)"), {"t": get_current_tenant_id()}
                )
            ).scalar()
            order = Order(
                quote_id=quote.id,
                conversation_id=context.conversation_id,
                customer_id=context.customer_id,
                payment_method=payment_method,
                delivery_method=delivery_method,
                total=quote.total,
                idempotency_key=f"quote:{quote.id}",
                order_seq=order_seq,
                delivery_full_name=customer_data.get("full_name"),
                delivery_id_document=customer_data.get("id_document"),
                delivery_address=customer_data.get("address"),
                delivery_municipality=customer_data.get("municipality"),
                delivery_city=customer_data.get("city"),
                status="abierto",
                # transferencia/pago_en_linea se confirman después (comprobante manual o
                # webhook de pack-payments); contraentrega se da por pagado al entregar.
                payment_status="pendiente" if payment_method in ("transferencia", "pago_en_linea") else "pagado",
            )
            session.add(order)
            await session.flush()
            for item in items:
                session.add(
                    OrderItem(
                        order_id=order.id,
                        variant_id=item.variant_id,
                        quantity=item.quantity,
                        unit_price=item.unit_price,
                    )
                )
                await self._decrement_stock(session, item.variant_id, item.quantity)
            quote.status = "convertida"
            order_id = str(order.id)
            public_order_number = order.public_order_number

            if quote.applied_promotion_id is not None:
                promo = await get_owned(
                    session, Promotion, quote.applied_promotion_id, get_current_tenant_id()
                )
                if promo and promo.type == "campaña" and (promo.rules or {}).get("once_per_customer"):
                    session.add(
                        PromotionRedemption(
                            promotion_id=promo.id,
                            customer_id=context.customer_id,
                            order_id=order.id,
                        )
                    )

        result: dict[str, Any] = {
            "order_id": order_id,
            "status": "confirmed",
            "public_order_number": public_order_number,
            "total": float(quote.total),
            "payment_method": payment_method,
            "delivery_method": delivery_method,
            "entregado_en": f"{customer_data.get('address')}, {customer_data.get('municipality')}, {customer_data.get('city')}",
        }
        if payment_method == "transferencia":
            transfer_block = await _transfer_safe_append()
            template = getattr(config, "order_confirmed_transfer_message", "") or ""
            if transfer_block:
                message = template.format(
                    public_order_number=public_order_number, transfer_block=transfer_block
                )
            else:
                message = (
                    f"Tu pedido {public_order_number} quedó confirmado por transferencia. "
                    "Te enviaremos los datos para pagar en breve."
                )
            result["_safe_append"] = message
        else:
            symbol = getattr(config, "currency_symbol", "$") or "$"
            template = getattr(config, "order_confirmed_message", "") or ""
            result["_safe_append"] = template.format(
                public_order_number=public_order_number,
                total=_money(quote.total, symbol),
            )
        return result

    async def agregar_item_pedido(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        order_id = str(arguments.get("order_id", ""))
        items = arguments.get("items") or []
        if not items or not isinstance(items, list):
            return {"error": "Necesito al menos un producto para agregar."}
        if any(not isinstance(item, dict) for item in items):
            return {"error": "Cada producto a agregar debe traer variant_id y quantity."}
        if _parse_uuid(order_id) is None:
            return {"error": "Pedido no encontrado."}

        async with get_session() as session:
            order = (
                await session.execute(
                    select(Order).where(
                        Order.id == order_id, Order.tenant_id == get_current_tenant_id()
                    )
                )
            ).scalar_one_or_none()
            if order is None:
                return {"error": "Pedido no encontrado."}
            if order.status != "abierto":
                return {"error": "El pedido ya no está abierto; no se pueden agregar productos."}
            existing_batch = (
                await session.execute(
                    select(OrderItemBatch).where(
                        OrderItemBatch.idempotency_key == context.idempotency_key,
                        OrderItemBatch.tenant_id == get_current_tenant_id(),
                    )
                )
            ).scalar_one_or_none()
            if existing_batch is not None:
                return {"order_id": order_id, "status": "duplicate"}

            additions: list[tuple[ProductVariant, int, float]] = []
            add_total = 0.0
            for item in items:
                variant_ref = str(item.get("variant_id", ""))
                try:
                    quantity = int(item.get("quantity", 0))
                except (TypeError, ValueError):
                    return {"error": "Cantidad inválida."}
                if quantity <= 0:
                    return {"error": "Las cantidades deben ser positivas."}
                data = await _get_variant_with_stock(variant_ref)
                if data is None or not data["variant"].active:
                    return {"error": "Uno de los productos no está disponible."}
                if data["stock"] < quantity:
                    return {"error": f"No hay stock suficiente de {data['product'].name}."}
                additions.append((data["variant"], quantity, data["price"]))
                add_total += data["price"] * quantity

            new_total = _float(order.total) + add_total
            config = await _plugin_config()
            threshold = getattr(config, "monto_alto_threshold", 1_000_000) or 1_000_000
            if new_total > threshold:
                return {
                    "error": "El total supera el monto máximo permitido y requiere aprobación humana.",
                    "escalate": True,
                }

            batch = OrderItemBatch(order_id=order.id, idempotency_key=context.idempotency_key)
            session.add(batch)
            for variant, quantity, price in additions:
                session.add(
                    OrderItem(
                        order_id=order.id,
                        variant_id=variant.id,
                        quantity=quantity,
                        unit_price=price,
                    )
                )
                await self._decrement_stock(session, variant.id, quantity)
            order.total = new_total

        return {
            "order_id": order_id,
            "status": "updated",
            "nuevo_total": round(new_total, 2),
            "agregados": [{"sku": v.sku, "quantity": q} for v, q, _p in additions],
        }

    async def consultar_estado_pedido(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        raw = str(arguments.get("public_order_number", "")).strip()
        match = re.match(r"^\s*OC[-\s]?(\d{1,6})\s*$", raw, re.IGNORECASE)
        if not match:
            return {
                "found": False,
                "hint": "El número de pedido tiene el formato OC-0001. Pídelo al cliente.",
            }
        public_number = f"OC-{int(match.group(1)):04d}"
        config = await _plugin_config()

        async with get_session() as session:
            tenant_id = get_current_tenant_id()
            order = (
                await session.execute(
                    select(Order).where(
                        Order.customer_id == context.customer_id,
                        Order.public_order_number == public_number,
                        Order.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if order is None:
                return {"found": False, "hint": "No encontré un pedido con ese número para este cliente."}
            items = (
                await session.execute(
                    select(OrderItem).where(
                        OrderItem.order_id == order.id, OrderItem.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            item_details = (
                await session.execute(
                    select(ProductVariant, Product)
                    .join(Product, Product.id == ProductVariant.product_id)
                    .where(
                        ProductVariant.id.in_([i.variant_id for i in items]),
                        ProductVariant.tenant_id == tenant_id,
                    )
                )
            ).all()
            by_id = {v.id: p for v, p in item_details}

        status_labels = getattr(config, "order_status_visible", {}) or {}
        status_label = status_labels.get(order.status, order.status)
        return {
            "found": True,
            "public_order_number": order.public_order_number,
            "status": status_label,
            "payment_status": "pagado" if order.payment_status == "pagado" else "pendiente de pago",
            "delivery_method": "domicilio" if order.delivery_method == "domicilio" else "recoger en tienda",
            "items": [
                {
                    "product": by_id.get(i.variant_id).name if by_id.get(i.variant_id) else "Producto",
                    "quantity": i.quantity,
                    "unit_price": float(i.unit_price),
                }
                for i in items
            ],
            "total": _float(order.total),
            "created_at": order.created_at.isoformat(),
        }

    @staticmethod
    async def _decrement_stock(session, variant_id, quantity: int) -> None:  # type: ignore[no-untyped-def]
        inventory = (
            await session.execute(
                select(Inventory).where(
                    Inventory.variant_id == variant_id,
                    Inventory.tenant_id == get_current_tenant_id(),
                )
            )
        ).scalar_one_or_none()
        if inventory is None:
            session.add(Inventory(variant_id=variant_id, stock_quantity=max(0, -quantity)))
            return
        inventory.stock_quantity = max(inventory.stock_quantity - quantity, 0)
        inventory.last_synced_at = _now()