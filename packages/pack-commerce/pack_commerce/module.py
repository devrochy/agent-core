"""CRMModule: el BaseModule del plugin.

Registra skills (catalog, order, lead), afirmaciones de cierre, eventos y el
panel admin del CRM (Leads, Productos, Categorías, Promociones, Pedidos).

El pipeline de venta es: lead -> cotización (con promoción) -> pedido.
No hay deals.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

# Importar `jobs` a nivel de módulo registra los `@register_cron` en Celery
# (worker y beat) apenas se escanea el plugin -- no dentro de `on_activate()`,
# que solo corre en el proceso API y dejaba los crons de los packs sin
# programar/registrar en el worker. La ejecución sigue condicionada a que el
# plugin esté activo (guard del decorador).
import pack_commerce.jobs  # noqa: F401
from core.db.session import get_owned, get_session
from core.mcp.types import Afirmacion, BaseModule
from core.shared.dinero import MONEY_IN_TEXT, MONEY_KEYS, parse_money  # noqa: F401 (API del pack)
from core.shared.events import ON_CONVERSATION_STARTED, ON_PROCEDURE_SLOTS_SET
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_commerce import api as leads_api
from pack_commerce.admin_api import router as admin_api_router
from pack_commerce.api import router as leads_router
from pack_commerce.config import CRMConfig
from pack_commerce.models import (
    Category,
    CategoryComplement,
    Inventory,
    Lead,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Promotion,
    Quote,
)
from pack_commerce.skills import (
    CatalogSkill,
    LeadSkill,
    OrderSkill,
    classify_customer,
    referencias_vigentes,
)

if TYPE_CHECKING:
    from core.mcp.host import MCPHost
    from core.primeros_pasos.contrato import ContenidoCargado

log = get_logger(__name__)

PLUGIN_ID = "pack_commerce"

# Frases que afirman que una acción de negocio quedó resuelta -> deben
# corresponder a una tool REALMENTE llamada en ESTE turno (lo comprueba el
# verificador del core, ADR-0119, con estas afirmaciones), o es una "phantom
# completion": el modelo dice que hizo algo sin haberlo hecho. Encontrado en
# vivo (tenant yopoldo, 2026-09-11): tras 2 intentos fallidos de generar_cotizacion
# (variant_id inventado), el modelo terminó diciendo "cerré tu pedido, en un
# momentico el asesor te pasa el link" sin haber llamado crear_pedido NI
# al generar el link NI escalado de verdad -- el cliente quedaba esperando algo
# que nunca iba a pasar. Mismo principio que la verificación de montos, pero
# para acciones.
#
# Los patrones usan raíces (`\w*`) en vez de conjugaciones exactas: la primera
# versión de este guardrail (solo "cerré tu pedido"/"te envío el link") la
# esquivó SIN querer el propio modelo al decir "Cerramos tu pedido... Ahora
# mismo te genero el link" en un incidente posterior (mismo día) -- ninguna
# forma exacta de "decirlo" cubre todas las conjugaciones que un LLM puede
# elegir, así que se prioriza la raíz del verbo + la palabra clave del tema
# por sobre la frase exacta.
COMPLETION_CLAIMS: dict[str, tuple[re.Pattern[str], set[str]]] = {
    "pedido": (
        re.compile(
            # El artículo va con su propio espacio: `(?:tu)?\s*` era cuadrático ante
            # una racha de espacios (revisión de seguridad de HU-ARCH-29).
            r"cerr\w*\s+(?:(?:tu|su|el|los?|la)\s+)?pedido|"
            # Voz activa en pasado: "Ya creé tu pedido".
            r"\b(?:cre|gener|registr)é\s+(?:(?:tu|su|el)\s+)?pedido|"
            r"pedido\b(?:\W+\w+){0,3}\W+(?:confirmad\w*|cerrad\w*|cread\w*|generad\w*)|"
            # Más lejos (hasta 6 palabras), solo con el auxiliar pegado al participio en
            # masculino. Test, tenant solvershub (2026-10-11): "Tu pedido del plan Premium quedó
            # creado" sin `crear_pedido` pasó con el tope de 3. El auxiliar y el masculino dejan
            # fuera los resúmenes ("…ya quedó generada la cotización", "datos confirmados").
            r"pedido\b(?:\W+\w+){0,5}\W+(?:qued[oó]|est[aá]|fue)\s+(?:ya\s+)?"
            r"(?:confirmad|cerrad|cread|generad)o\b|"
            # El participio antes del sustantivo: "quedó creado tu pedido".
            r"\b(?:qued[oó]|est[aá]|fue)\s+(?:ya\s+)?(?:confirmad|cerrad|cread|generad)o\s+"
            r"(?:tu|su|el)\s+pedido",
            re.IGNORECASE,
        ),
        {"crear_pedido", "consultar_estado_pedido", "confirmar_pago_pedido"},
    ),
    "link_de_pago": (
        # Cualquier mención de "link de/para pago(ar)" en el texto LIBRE del
        # modelo (nunca en el _safe_append, que corre después de los
        # guardrails) implica que se está anunciando -- si no llamó la tool,
        # es una promesa vacía sin importar el verbo que haya usado.
        re.compile(r"link\s+(?:de\s+pago|para\s+pagar)", re.IGNORECASE),
        # Además de `confirmar_pago_pedido`, lo respalda cualquier tool del turno que
        # haya devuelto un link (`_LINK_CON_URL`): el de una suscripción, el de un
        # cambio de plan o el de pagos en línea. Así el pack no lista tools de otros
        # packs y un link nuevo no exige tocar esta lista (HU-ARCH-29).
        {"confirmar_pago_pedido"},
    ),
    # HU-ARCH-29 (journeys, 2026-10-09): "Ya dejé tu cotización aceptada" sin haber
    # llamado ninguna tool. Armar la cotización y aceptarla son dos acciones distintas.
    "cotizacion_generada": (
        re.compile(
            r"\b(?:arm|gener)[eé]\s+(?:de\s+nuevo\s+)?(?:tu|la)\s+cotizaci[oó]n|"
            r"\bcotizaci[oó]n\b(?:\W+\w+){0,2}\W+(?:armad|generad)\w*|"
            # El participio antes del sustantivo, anclado a un auxiliar: "quedó generada
            # la cotización" afirma; "una vez generada la cotización…" no (2026-10-10).
            r"\b(?:qued[oó]|est[aá]|fue)\s+(?:ya\s+)?(?:armad|generad)a\s+(?:tu|la)\s+cotizaci[oó]n",
            re.IGNORECASE,
        ),
        # `cotizar_plan` (HU-AOS-07) es la misma cotización con una unidad, la del procedimiento
        # `contratar_plan`: "te armé la cotización" tras cotizar_plan es verdad (pass^4, 2026-10-18).
        {"generar_cotizacion", "cotizar_plan"},
    ),
    "cotizacion_aceptada": (
        re.compile(
            r"\b(?:acept|confirm)[eé]\s+(?:tu|la)\s+cotizaci[oó]n|"
            r"\bcotizaci[oó]n\b(?:\W+\w+){0,2}\W+(?:aceptad|confirmad)\w*|"
            # Test, tenant solvershub (2026-10-10): "ya quedó confirmada la cotización"
            # sin `confirmar_cotizacion` pasó el verificador (`cierres: 0`).
            r"\b(?:qued[oó]|est[aá]|fue)\s+(?:ya\s+)?(?:aceptad|confirmad)a\s+(?:tu|la)\s+cotizaci[oó]n",
            re.IGNORECASE,
        ),
        {"confirmar_cotizacion"},
    ),
    # Test, tenant solvershub (2026-10-10): con el `ya_pagado` de `generar_link_pago` (un
    # error: la referencia era la del Basic ya pagado) el modelo dijo "el pedido OC-0001 ya
    # figura como pagado, así que no hace falta generar otro", y luego "el pago sí quedó
    # registrado", sobre una compra del Premium que no tenía ni pedido. El estado de pago
    # se afirma con una consulta del turno; el bloque de referencias no es evidencia.
    "pago_registrado": (
        re.compile(
            r"\b(?:figur|aparec|qued|est|consta)\w*\s+(?:ya\s+)?(?:como\s+)?pagad[oa]s?\b|"
            r"\bya\s+(?:est[aá]|qued[oó])\s+pag[oa]\b|"
            # Con verbo: "medio de pago registrado: Nequi" no afirma nada.
            r"\bpago\s+(?:(?:ya|s[ií])\s+)*(?:qued[oó]|fue|est[aá]|ha\s+sido)\s+(?:ya\s+)?"
            r"(?:registrad|aprobad|acreditad)\w*|"
            r"\b(?:recib(?:imos|[ií]|i[oó])|se\s+(?:registr|acredit)[oó])\s+(?:tu|el)\s+pago\b|"
            # "Si ya pagaste, mandame el comprobante" es un condicional.
            r"(?<!si\s)\bya\s+(?:pagaste|(?:realizaste|hiciste)\s+el\s+pago)\b",
            re.IGNORECASE,
        ),
        {"consultar_estado_pedido", "sub_consultar"},
    ),
    "asesor_humano": (
        re.compile(
            r"comuniqu\w*\s+con\s+un\s+asesor|un\s+asesor\s+te\s+\w*|"
            r"pas[eé]\s+con\s+un\s+(?:asesor|humano)|en\s+un\s+momentico\s+te\s+contactan",
            re.IGNORECASE,
        ),
        {"escalate_to_human"},
    ),
    # Deuda registrada en el ROADMAP (sesión 2026-09-11, evals): el modelo a
    # veces DICE que guardó un dato de contacto del lead ("ya guardé tu
    # teléfono") sin haber llamado la tool -- mismo mecanismo, cierra ese
    # hallazgo también. Acotado a datos de CONTACTO específicamente (no
    # "guardé"/"registré" a secas, que también aplicaría a "guardé tu pedido"
    # y generaría falsos positivos contra la categoría "pedido" de arriba).
    "dato_de_contacto_guardado": (
        re.compile(
            r"(?:guard[eé]|registr[eé]|anot[eé])\s+(?:tu|el|los?|la)\s*"
            r"(?:tel[eé]fono|correo|email|nombre|direcci[oó]n|nu?mero)",
            re.IGNORECASE,
        ),
        {"crear_lead", "calificar_lead", "actualizar_lead", "update_customer_profile", "remember_fact"},
    ),
}


# Afirmaciones que también respalda una URL devuelta por una tool del turno.
_LINK_CON_URL = frozenset({"link_de_pago"})

# Datos de contacto de los procedimientos del pack (`tomar_pedido`, `contratar_plan`): con el motor
# activo, "anoté tu dirección" lo respalda el dato fijado en este turno (`Afirmacion.datos`).
_DATOS_DE_CONTACTO = frozenset(
    {"full_name", "id_document", "phone", "email", "address", "city", "municipality"}
)
_DATOS_QUE_RESPALDAN = {"dato_de_contacto_guardado": _DATOS_DE_CONTACTO}

_MAX_CUSTOM_COMPLETION_CLAIM_LINES = 50


def _parse_custom_completion_claims(raw: str) -> dict[str, tuple[re.Pattern[str], set[str]]]:
    """Parsea `CRMConfig.completion_claims_custom` (ADR-0025): una línea por
    claim, `etiqueta: frase uno | frase dos => tool_uno, tool_dos`. Nunca
    acepta regex crudo -- las frases se escapan (`re.escape`) antes de
    compilar, elimina el riesgo de ReDoS por diseño (no hay ninguna otra
    parte del repo que compile un regex proveniente de config/BD/form, así
    que no hay infraestructura de mitigación que reusar). Una línea mal
    formada se ignora con un warning, nunca revienta el guardrail (mismo
    criterio de fail-open que el resto de `core/orchestrator/guardrails.py`).

    Tope de líneas (defensa en profundidad, además del `max_length` del
    campo en `CRMConfig`): este guardrail corre en CADA turno de
    conversación de ESE tenant -- sin límite, una config con miles de
    líneas degradaría la latencia de ese tenant en cada turno (hallazgo de
    `auditor-multitenant`, no es fuga cross-tenant sino un costo de
    "vecino ruidoso" dentro del propio proceso compartido).
    """
    result: dict[str, tuple[re.Pattern[str], set[str]]] = {}
    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        if lineno > _MAX_CUSTOM_COMPLETION_CLAIM_LINES:
            log.warning("completion_claims_custom_truncado", limite=_MAX_CUSTOM_COMPLETION_CLAIM_LINES)
            break
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line or "=>" not in line:
            log.warning("completion_claim_custom_malformado", line=lineno)
            continue
        label, _, rest = line.partition(":")
        phrases_part, _, tools_part = rest.partition("=>")
        label = label.strip()
        phrases = [p.strip() for p in phrases_part.split("|") if p.strip()]
        tools = {t.strip() for t in tools_part.split(",") if t.strip()}
        if not label or not phrases or not tools:
            log.warning("completion_claim_custom_incompleto", line=lineno)
            continue
        result[label] = (
            re.compile("|".join(re.escape(p) for p in phrases), re.IGNORECASE),
            tools,
        )
    return result


admin_router = APIRouter(prefix="/crm", tags=[PLUGIN_ID])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_CORE_TEMPLATES = Path(__file__).resolve().parents[3] / "core" / "admin" / "templates"
templates = Jinja2Templates(directory=[str(_TEMPLATES_DIR), str(_CORE_TEMPLATES)])


# ------------------------------------------------------------------ helpers
def _render(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200):  # type: ignore[no-untyped-def]
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _money(value: Any) -> str:
    return f"${float(value or 0):,.0f}"


def _iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


async def _require_admin(request: Request) -> dict[str, Any] | None:
    """Contexto común del panel (incluye el selector de tenants) o None si no hay sesión.

    Reutiliza `core.admin.context.base_context` en vez de construir un contexto
    parcial: así todas las rutas del pack reciben `tenants`, `active_tenant_id` y
    `flash`, igual que las páginas del core. Los packs dependen del core.
    """
    from core.admin.context import base_context

    return await base_context(request, "crm")


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


# ------------------------------------------------------------------ dashboard
@admin_router.get("", response_class=HTMLResponse)
@admin_router.get("/", response_class=HTMLResponse)
async def crm_dashboard(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        leads = (
            await session.execute(select(Lead).where(Lead.tenant_id == tenant_id))
        ).scalars().all()
        products = (
            await session.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
        promotions = (
            await session.execute(select(Promotion).where(Promotion.tenant_id == tenant_id))
        ).scalars().all()
        orders = (
            await session.execute(select(Order).where(Order.tenant_id == tenant_id))
        ).scalars().all()
    ctx.update(
        kpi={
            "leads": len(leads),
            "productos": len(products),
            "promociones": len(promotions),
            "pedidos": len(orders),
        },
        recent_orders=sorted(orders, key=lambda o: o.created_at, reverse=True)[:5],
        _money=_money,
        _iso=_iso,
    )
    return _render(request, "crm/dashboard.html", ctx)


# ------------------------------------------------------------------ leads (vista por descriptor, Fase D)
async def _leads_list() -> list[dict[str, Any]]:
    """Filas para el renderizador por descriptor (Sprint 4, Fase D).

    Todo `Customer` del tenant ES un lead: no hace falta crear ni sincronizar
    nada (mismo diseño que agent-sale, ADR-036 "clasificación de cliente en 5
    niveles y rediseño de leads") — apenas alguien escribe por primera vez ya
    existe su `Customer` (`core/orchestrator/consumer.py::_resolve_conversation`),
    y `estado`/`segmento` se calculan en vivo contra su actividad real, sin
    depender de que el LLM llame ninguna tool. `pack_commerce_leads` sigue
    existiendo solo como enriquecimiento opcional de nombre/interés (lo que el
    LLM o un admin haya capturado en la conversación).

    Filtro explícito por tenant (defensa en profundidad, además de RLS): el panel
    es multi-tenant y una policy caída no debe convertir esto en una fuga."""
    from core.db.models import Conversation, Customer, Escalation, Message

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        customers = (
            await session.execute(
                select(Customer)
                .where(Customer.tenant_id == tenant_id, Customer.deleted_at.is_(None))
                .order_by(Customer.created_at.desc())
                .limit(200)
            )
        ).scalars().all()

        order_customer_ids = set(
            (
                await session.execute(
                    select(Order.customer_id).where(Order.tenant_id == tenant_id).distinct()
                )
            )
            .scalars()
            .all()
        )
        quote_customer_ids = set(
            (
                await session.execute(
                    select(Quote.customer_id).where(Quote.tenant_id == tenant_id).distinct()
                )
            )
            .scalars()
            .all()
        )
        escalated_customer_ids = set(
            (
                await session.execute(
                    select(Conversation.customer_id)
                    .join(Escalation, Escalation.conversation_id == Conversation.id)
                    .where(Conversation.tenant_id == tenant_id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        order_stats = {
            row.customer_id: (row.pedidos, row.ultima_compra)
            for row in (
                await session.execute(
                    select(
                        Order.customer_id,
                        func.count().label("pedidos"),
                        func.max(Order.created_at).label("ultima_compra"),
                    )
                    .where(Order.tenant_id == tenant_id, Order.status != "expirado")
                    .group_by(Order.customer_id)
                )
            ).all()
        }
        last_message_at = {
            row.customer_id: row.ultimo_mensaje
            for row in (
                await session.execute(
                    select(
                        Conversation.customer_id,
                        func.max(Message.created_at).label("ultimo_mensaje"),
                    )
                    .join(Message, Message.conversation_id == Conversation.id)
                    .where(Conversation.tenant_id == tenant_id)
                    .group_by(Conversation.customer_id)
                )
            ).all()
        }
        leads_by_customer = {
            lead.customer_id: lead
            for lead in (
                await session.execute(select(Lead).where(Lead.tenant_id == tenant_id))
            )
            .scalars()
            .all()
            if lead.customer_id is not None
        }

    rows: list[dict[str, Any]] = []
    for c in customers:
        lead = leads_by_customer.get(c.id)
        if c.id in order_customer_ids:
            estado = "con_pedido"
        elif c.id in escalated_customer_ids:
            estado = "escalada"
        elif c.id in quote_customer_ids:
            estado = "con_cotizacion"
        else:
            estado = "sin_actividad_comercial"
        pedidos, _ultima_compra = order_stats.get(c.id, (0, None))
        activity = last_message_at.get(c.id) or c.created_at
        rows.append(
            {
                "_id": str(c.id),
                "name": (lead.name if lead else None) or c.name or c.external_id,
                "interest": (lead.interest if lead else "") or "",
                "segment": await classify_customer(c.id),
                "estado": estado,
                "pedidos": pedidos,
                "last_activity": _iso(activity) if activity else "",
            }
        )
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    return rows


async def _leads_save(record_id: str | None, values: dict[str, Any]) -> None:
    """`record_id` es el `customer.id` (no un `lead.id`): estado/segmento ya se
    calculan solos, lo único editable a mano es el enriquecimiento de
    nombre/interés — se crea la fila de `Lead` si todavía no existía."""
    if record_id is None:
        return
    try:
        customer_uuid = uuid.UUID(record_id)
    except ValueError:
        return
    from core.db.models import Customer

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        # customer_id es FK lógica (sin constraint física) — validar que el
        # customer exista y sea de este tenant antes de crear/tocar el Lead,
        # para no dejar una referencia colgante hacia otro tenant.
        customer = (
            await session.execute(
                select(Customer).where(
                    Customer.id == customer_uuid, Customer.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if customer is None:
            return

        lead = (
            await session.execute(
                select(Lead).where(Lead.customer_id == customer_uuid, Lead.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if lead is None:
            lead = Lead(customer_id=customer_uuid, idempotency_key=f"manual:{customer_uuid}")
            session.add(lead)
        if "name" in values:
            lead.name = str(values["name"]) or None
        if "interest" in values:
            lead.interest = str(values["interest"] or "")
        lead.last_activity_at = datetime.now(UTC)


# ------------------------------------------------------------------ productos
@admin_router.get("/productos", response_class=HTMLResponse)
async def crm_products(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        products = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name)
            )
        ).scalars().all()
        variants = (
            await session.execute(select(ProductVariant).where(ProductVariant.tenant_id == tenant_id))
        ).scalars().all()
        inventory = (
            await session.execute(select(Inventory).where(Inventory.tenant_id == tenant_id))
        ).scalars().all()
        categories = (
            await session.execute(
                select(Category)
                .where(Category.deleted_at.is_(None), Category.tenant_id == tenant_id)
                .order_by(Category.name)
            )
        ).scalars().all()
    cat_by_id = {c.id: c for c in categories}
    inv_by_variant = {i.variant_id: i for i in inventory}
    variant_by_product: dict[str, list[ProductVariant]] = {}
    for v in variants:
        variant_by_product.setdefault(str(v.product_id), []).append(v)
    ctx.update(
        products=[
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "category": cat_by_id[p.category_id].name if p.category_id in cat_by_id else "Sin categoría",
                "price_raw": float((variant_by_product.get(str(p.id)) or [None])[0].price or 0),
                "variants": [
                    {
                        "id": v.id,
                        "sku": v.sku,
                        "attributes": v.attributes or {},
                        "price": _money(v.price),
                        "stock": inv_by_variant[v.id].stock_quantity if v.id in inv_by_variant else 0,
                        "active": v.active,
                    }
                    for v in variant_by_product.get(str(p.id), [])
                ],
            }
            for p in products
        ],
        categories=[{"id": c.id, "name": c.name} for c in categories],
        _money=_money,
    )
    return _render(request, "crm/productos.html", ctx)


@admin_router.post("/productos/guardar")
async def product_save(
    request: Request,
    product_id: str = Form(default=""),
    name: str = Form(default=""),
    description: str = Form(default=""),
    category_id: str = Form(default=""),
    sku: str = Form(default=""),
    price: float = Form(default=0),
    stock: int = Form(default=0),
):
    if await _require_admin(request) is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        # Defensa en profundidad, mismo criterio que `parent_id` en
        # category_save: no dejar `Product.category_id` colgando hacia una
        # categoría de otro tenant (o inexistente) sin validarla primero.
        resolved_category_id: uuid.UUID | None = None
        if category_id:
            category = await get_owned(session, Category, category_id, tenant_id)
            if category is not None:
                resolved_category_id = category.id

        if product_id:
            product = await get_owned(session, Product, product_id, tenant_id)
            if product is not None:
                product.name = name
                product.description = description
                product.category_id = resolved_category_id
                variant = (
                    await session.execute(
                        select(ProductVariant).where(
                            ProductVariant.product_id == product.id,
                            ProductVariant.tenant_id == tenant_id,
                        )
                    )
                ).scalars().first()
                if variant is not None:
                    variant.sku = sku or variant.sku
                    variant.price = price or variant.price
                    inv = (
                        await session.execute(
                            select(Inventory).where(
                                Inventory.variant_id == variant.id,
                                Inventory.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if inv is None:
                        session.add(Inventory(variant_id=variant.id, stock_quantity=stock))
                    else:
                        inv.stock_quantity = stock
        else:
            product = Product(name=name, description=description, category_id=resolved_category_id)
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                product_id=product.id, sku=sku or f"SKU-{product.id.hex[:8]}", price=price
            )
            session.add(variant)
            await session.flush()
            session.add(Inventory(variant_id=variant.id, stock_quantity=stock))
            product.has_variants = False
    return RedirectResponse("/crm/productos?flash=Producto guardado", status_code=303)


@admin_router.post("/productos/importar-csv")
async def product_import_csv(request: Request, file: Annotated[UploadFile, File()]):
    if await _require_admin(request) is None:
        return _login_redirect()
    content = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    if not rows or "name" not in rows[0] or "price" not in rows[0]:
        return RedirectResponse(
            "/crm/productos?flash=CSV inválido: requiere columnas name y price&flash_type=danger",
            status_code=303,
        )
    created = 0
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            sku = (row.get("sku") or "").strip() or None
            price = float(row.get("price") or 0)
            stock = int(float(row.get("stock") or 0))
            description = (row.get("description") or "").strip()
            cat_name = (row.get("category") or "").strip()
            product = Product(name=name, description=description)
            if cat_name:
                cat = (
                    await session.execute(
                        select(Category).where(
                            Category.name == cat_name,
                            Category.deleted_at.is_(None),
                            Category.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if cat is not None:
                    product.category_id = cat.id
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                product_id=product.id, sku=sku or f"SKU-{product.id.hex[:8]}", price=price
            )
            session.add(variant)
            await session.flush()
            session.add(Inventory(variant_id=variant.id, stock_quantity=stock))
            created += 1
    return RedirectResponse(
        f"/crm/productos?flash={created} productos importados", status_code=303
    )


# ------------------------------------------------------------------ categorías
@admin_router.get("/categorias", response_class=HTMLResponse)
async def crm_categories(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        categories = (
            await session.execute(
                select(Category)
                .where(Category.deleted_at.is_(None), Category.tenant_id == tenant_id)
                .order_by(Category.sort_order, Category.name)
            )
        ).scalars().all()
        complements = (
            await session.execute(
                select(CategoryComplement).where(CategoryComplement.tenant_id == tenant_id)
            )
        ).scalars().all()
    comp_by_cat: dict[str, list[str]] = {}
    for c in complements:
        comp_by_cat.setdefault(str(c.category_id), []).append(str(c.complementary_category_id))
    cat_by_id = {str(c.id): c for c in categories}

    def build(parent_id: uuid.UUID | None) -> list[dict[str, Any]]:
        nodes = []
        for c in categories:
            if c.parent_id == parent_id:
                nodes.append(
                    {
                        "id": c.id,
                        "parent_id": c.parent_id,
                        "name": c.name,
                        "sort_order": c.sort_order,
                        "active": c.active,
                        "children": build(c.id),
                        "complements": [cat_by_id[x].name for x in comp_by_cat.get(str(c.id), []) if x in cat_by_id],
                    }
                )
        return nodes

    ctx.update(tree=build(None), categories=categories, _money=_money)
    return _render(request, "crm/categorias.html", ctx)


@admin_router.post("/categorias/guardar")
async def category_save(
    request: Request,
    category_id: str = Form(default=""),
    name: str = Form(default=""),
    parent_id: str = Form(default=""),
    sort_order: int = Form(default=0),
    complements: str = Form(default=""),
):
    if await _require_admin(request) is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        parent = uuid.UUID(parent_id) if parent_id else None
        if parent is not None:
            # Defensa en profundidad además de RLS: filtro tenant_id explícito
            # en vez de session.get() a secas -- si `parent` no existe o es de
            # otro tenant, devuelve None en ambos casos, se rechaza en vez de
            # asignar igual (dejaría un parent_id colgando hacia otro tenant).
            # Una categoría eliminada (soft-delete) tampoco es un padre válido.
            parent_category = await get_owned(session, Category, parent, tenant_id)
            if parent_category is None or parent_category.deleted_at is not None:
                return RedirectResponse(
                    "/crm/categorias?flash=Categoría padre no encontrada&flash_type=danger",
                    status_code=303,
                )
        if category_id:
            category = await get_owned(session, Category, category_id, tenant_id)
            if category is None:
                # Antes de este fix, `session.get()` también devolvía `None`
                # acá sin filtro explícito -- el código seguía de largo y
                # reventaba más abajo con `category.id` sin definir (500 en
                # vez de un flash controlado). Mismo criterio que
                # `category_delete` para "no existe/es de otro tenant".
                return RedirectResponse(
                    "/crm/categorias?flash=Categoría no encontrada&flash_type=danger",
                    status_code=303,
                )
            if parent is not None:
                # Una categoría no puede ser su propia padre ni un descendiente
                # de sí misma (eso la dejaría inalcanzable en el árbol del listado).
                ancestor_id: uuid.UUID | None = parent
                seen: set[uuid.UUID] = set()
                while ancestor_id is not None and ancestor_id not in seen:
                    if ancestor_id == category.id:
                        return RedirectResponse(
                            "/crm/categorias?flash=Una categoría no puede ser su propia"
                            " categoría padre ni descendiente de sí misma&flash_type=danger",
                            status_code=303,
                        )
                    seen.add(ancestor_id)
                    ancestor = await get_owned(session, Category, ancestor_id, tenant_id)
                    ancestor_id = ancestor.parent_id if ancestor is not None else None
            category.name = name
            category.parent_id = parent
            category.sort_order = sort_order
        else:
            category = Category(name=name, parent_id=parent, sort_order=sort_order)
            session.add(category)
            await session.flush()

        if category_id:
            await session.execute(
                CategoryComplement.__table__.delete().where(
                    CategoryComplement.category_id == category.id,
                    CategoryComplement.tenant_id == tenant_id,
                )
            )
        for raw in complements.split(","):
            comp = raw.strip()
            if not comp:
                continue
            try:
                comp_uuid = uuid.UUID(comp)
            except ValueError:
                continue
            if comp_uuid == category.id:
                continue
            # Defensa en profundidad, mismo criterio que `parent_id` más
            # arriba: no encadenar `complementary_category_id` a un UUID
            # ajeno sin verificar que exista y sea del tenant activo.
            complement = await get_owned(session, Category, comp_uuid, tenant_id)
            if complement is None:
                continue
            session.add(
                CategoryComplement(category_id=category.id, complementary_category_id=comp_uuid)
            )
    return RedirectResponse("/crm/categorias?flash=Categoría guardada", status_code=303)


@admin_router.post("/categorias/eliminar")
async def category_delete(request: Request, category_id: str = Form(default="")):
    """Soft-delete (invariante nº 8): marca deleted_at, nunca borra la fila.

    Se niega si tiene hijos activos — reparentarlos automáticamente sería
    sorprendente, y dejarlos huérfanos los volvería inalcanzables en el árbol
    (mismo problema ya arreglado para el ciclo de auto-referencia)."""
    if await _require_admin(request) is None:
        return _login_redirect()
    try:
        category_uuid = uuid.UUID(category_id)
    except ValueError:
        return RedirectResponse("/crm/categorias?flash=Categoría inválida&flash_type=danger", status_code=303)
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        category = await get_owned(session, Category, category_uuid, tenant_id)
        if category is None or category.deleted_at is not None:
            return RedirectResponse(
                "/crm/categorias?flash=Categoría no encontrada&flash_type=danger", status_code=303
            )
        has_children = (
            await session.execute(
                select(Category).where(
                    Category.parent_id == category_uuid,
                    Category.deleted_at.is_(None),
                    Category.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if has_children is not None:
            return RedirectResponse(
                "/crm/categorias?flash=No se puede eliminar: tiene subcategorías activas."
                " Movelas o eliminalas primero.&flash_type=danger",
                status_code=303,
            )
        category.deleted_at = datetime.now(UTC)
    return RedirectResponse("/crm/categorias?flash=Categoría eliminada", status_code=303)


# ------------------------------------------------------------------ promociones
@admin_router.get("/promociones", response_class=HTMLResponse)
async def crm_promotions(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        promotions = (
            await session.execute(
                select(Promotion)
                .where(Promotion.tenant_id == tenant_id)
                .order_by(Promotion.created_at.desc())
            )
        ).scalars().all()
        categories = (
            await session.execute(
                select(Category)
                .where(Category.deleted_at.is_(None), Category.tenant_id == tenant_id)
                .order_by(Category.name)
            )
        ).scalars().all()
        products = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name)
            )
        ).scalars().all()
    ctx.update(
        promotions=[
            {
                "id": p.id,
                "type": p.type,
                "label": (p.rules or {}).get("label", ""),
                "discount_pct": (p.rules or {}).get("discount_pct", ""),
                "discount_raw": float((p.rules or {}).get("discount_pct", 0) or 0),
                "active": p.active,
                "valid_from": _iso(p.valid_from),
                "valid_to": _iso(p.valid_to),
                "segments": p.eligible_segments or [],
                "once_per_customer": bool((p.rules or {}).get("once_per_customer")),
            }
            for p in promotions
        ],
        categories=[{"id": c.id, "name": c.name} for c in categories],
        products=[{"id": p.id, "name": p.name} for p in products],
    )
    return _render(request, "crm/promociones.html", ctx)


@admin_router.post("/promociones/guardar")
async def promotion_save(
    request: Request,
    promotion_id: str = Form(default=""),
    name: str = Form(default=""),
    promo_type: str = Form(default="temporada"),
    discount_pct: float = Form(default=0),
    once_per_customer: str = Form(default=""),
    valid_from: str = Form(default=""),
    valid_to: str = Form(default=""),
    category_id: str = Form(default=""),
    product_id: str = Form(default=""),
    segments: str = Form(default=""),
):
    if await _require_admin(request) is None:
        return _login_redirect()
    rules: dict[str, Any] = {"label": name, "discount_pct": discount_pct}
    if promo_type == "campaña" and once_per_customer == "on":
        rules["once_per_customer"] = True
    segments_list = [s.strip() for s in segments.split(",") if s.strip()] or None

    def parse_dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        # Defensa en profundidad, mismo criterio que `parent_id`/`category_id`
        # en category_save/product_save: no dejar category_id/product_id
        # colgando hacia una fila de otro tenant sin validarla primero.
        resolved_category_id: uuid.UUID | None = None
        if category_id:
            cat = await get_owned(session, Category, category_id, tenant_id)
            if cat is not None:
                resolved_category_id = cat.id
        resolved_product_id: uuid.UUID | None = None
        if product_id:
            prod = await get_owned(session, Product, product_id, tenant_id)
            if prod is not None:
                resolved_product_id = prod.id

        if promotion_id:
            promo = await get_owned(session, Promotion, promotion_id, tenant_id)
            if promo is not None:
                promo.type = promo_type
                promo.rules = rules
                promo.valid_from = parse_dt(valid_from)
                promo.valid_to = parse_dt(valid_to)
                promo.category_id = resolved_category_id
                promo.product_id = resolved_product_id
                promo.eligible_segments = segments_list
        else:
            promo = Promotion(
                type=promo_type,
                rules=rules,
                valid_from=parse_dt(valid_from),
                valid_to=parse_dt(valid_to),
                category_id=resolved_category_id,
                product_id=resolved_product_id,
                eligible_segments=segments_list,
            )
            session.add(promo)
    return RedirectResponse("/crm/promociones?flash=Promoción guardada", status_code=303)


@admin_router.post("/promociones/{promotion_id}/toggle")
async def promotion_toggle(request: Request, promotion_id: uuid.UUID):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        promo = await get_owned(session, Promotion, promotion_id, get_current_tenant_id())
        if promo is not None:
            promo.active = not promo.active
    return RedirectResponse("/crm/promociones?flash=Promoción actualizada", status_code=303)


# ------------------------------------------------------------------ pedidos
@admin_router.get("/pedidos", response_class=HTMLResponse)
async def crm_orders(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        orders = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tenant_id)
                .order_by(Order.created_at.desc())
                .limit(200)
            )
        ).scalars().all()
        items = (
            await session.execute(select(OrderItem).where(OrderItem.tenant_id == tenant_id))
        ).scalars().all()
        variants = (
            await session.execute(
                select(ProductVariant, Product)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(ProductVariant.tenant_id == tenant_id, Product.tenant_id == tenant_id)
            )
        ).all()
    var_by_id = {v.id: p for v, p in variants}
    items_by_order: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        items_by_order.setdefault(str(i.order_id), []).append(
            {
                "name": var_by_id[i.variant_id].name if i.variant_id in var_by_id else "Producto",
                "quantity": i.quantity,
                "unit_price": _money(i.unit_price),
            }
        )
    ctx.update(
        orders=[
            {
                "id": o.id,
                "public_order_number": o.public_order_number,
                "total": _money(o.total),
                "total_raw": float(o.total),
                "status": o.status,
                "payment_status": o.payment_status,
                "payment_method": o.payment_method,
                "delivery_method": o.delivery_method,
                "delivery_address": o.delivery_address,
                "delivery_full_name": o.delivery_full_name,
                "created_at": _iso(o.created_at),
                "line_items": items_by_order.get(str(o.id), []),
            }
            for o in orders
        ],
        _money=_money,
    )
    return _render(request, "crm/pedidos.html", ctx)


@admin_router.post("/pedidos/{order_id}/estado")
async def order_update_status(request: Request, order_id: uuid.UUID, status: str = Form(...)):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        order = await get_owned(session, Order, order_id, get_current_tenant_id())
        if order is not None and status in ("abierto", "despachado", "entregado", "cancelado", "expirado"):
            order.status = status
            order.status_changed_at = datetime.now(UTC)
    return RedirectResponse("/crm/pedidos?flash=Estado actualizado", status_code=303)


# ------------------------------------------------------------------ módulo
class CRMModule(BaseModule):
    id = PLUGIN_ID
    name = "CRM"
    version = "2.3.0"
    description = "Leads, catálogo (menú), promociones y pedidos para el negocio"
    display_name = "Comercio"
    category = "Ventas"
    min_plan = "basic"
    action_summary: ClassVar[list[str]] = ["Cotizar", "Tomar pedidos", "Aplicar promociones", "Gestionar leads"]
    setup_guide = "Carga tu catálogo (CSV o a mano) y define cómo cierras pedidos y cobras."
    requires_config = True
    config_schema = CRMConfig
    # Salida a red (ADR-0052): solo el `siteverify` de Turnstile del intake de leads.
    network_allowlist: ClassVar[tuple[str, ...]] = leads_api.NETWORK
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Dashboard", "path": "/crm/"},
        {"label": "Leads", "path": "/admin/packs/pack_commerce/leads"},
        {"label": "Productos", "path": "/crm/productos"},
        {"label": "Categorías", "path": "/crm/categorias"},
        {"label": "Promociones", "path": "/crm/promociones"},
        {"label": "Pedidos", "path": "/crm/pedidos"},
    ]
    admin_router = admin_router

    def admin_api_routes(self) -> list[Any]:
        """API admin consumida por el panel nuevo (ADR-0079)."""
        return [admin_api_router]
    declared_skills: list[type] = [  # noqa: RUF012
        LeadSkill,
        CatalogSkill,
        OrderSkill,
    ]

    def public_routes(self) -> list[Any]:
        """API pública del pack (ADR-0057): intake de leads desde landings."""
        return [leads_router]

    async def seed_data(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
        """Catálogo de ejemplo o del negocio (ADR-0083, ADR-0110). Idempotente."""
        from pack_commerce.seed import seed_catalog

        return await seed_catalog(tenant_id, data)

    async def contenido_cargado(self, tenant_id: uuid.UUID) -> ContenidoCargado:
        """Productos a la venta (con alguna variante activa) para el paso «catálogo o servicios»
        de los primeros pasos (HU-SH-06, ADR-0124). Solo lee."""
        from core.primeros_pasos.contrato import ContenidoCargado
        from core.shared.tenant import tenant_scope

        a_la_venta = (
            select(ProductVariant.id)
            .where(
                ProductVariant.tenant_id == tenant_id,
                ProductVariant.product_id == Product.id,
                ProductVariant.active.is_(True),
            )
            .exists()
        )
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                cantidad, primero = (
                    await session.execute(
                        select(func.count(Product.id), func.min(Product.created_at)).where(
                            Product.tenant_id == tenant_id, a_la_venta
                        )
                    )
                ).one()
        return ContenidoCargado(tipo="catalogo", cantidad=int(cantidad or 0), primero_en=primero, seccion=PLUGIN_ID)

    def on_activate(self, host: MCPHost) -> None:
        host.register_skill(LeadSkill())
        host.register_skill(CatalogSkill())
        host.register_skill(OrderSkill())
        # Montos y afirmaciones de cierre los verifica el core (ADR-0119, HU-ARCH-29);
        # reemplaza a `crm_amount_check` y `crm_completion_check`.
        host.register_afirmaciones(self._afirmaciones)
        # Procedimientos de comercio (HU-AOS-07): datos del pack (`procedures/*.json`), sus
        # resolvers y el resumen de `confirmar_cotizacion`. Solo los usa el motor del core con
        # `tenants.aos_procedures`; con la bandera apagada nada cambia.
        from pack_commerce.procedimientos import registrar as registrar_procedimientos

        registrar_procedimientos(host)
        # La cotización en curso y el último pedido de la conversación, para el prompt
        # (ADR-0130): sin esto el modelo no ve el `quote_id` en el turno siguiente.
        host.register_referencias(referencias_vigentes)
        # Catálogo de planes para el cambio de plan del core (HU-SUB-07, ADR-0111).
        from core.subscriptions import plan_catalog_registry
        from pack_commerce.planes import CatalogoDePlanes

        plan_catalog_registry.register("pack_commerce", CatalogoDePlanes())
        # Datos del pedido para el alta automática de negocios (HU-SH-03, ADR-0121).
        from core.provisioning.fuentes import fuentes_de_alta
        from pack_commerce.altas import DatosDeAltaDelPedido

        fuentes_de_alta.register("pack_commerce", DatosDeAltaDelPedido())

        # Vistas por descriptor (Fase D, Sprint 4): la del panel se sirve con HTML
        # genérico — cero plantillas/rutas propias del pack.
        self._register_views()

        # La suscripción se registra SIEMPRE: la decisión de auto_create_lead se
        # evalúa por tenant en el evento (la config activada no es global — vive
        # en plugin_configs y un tenant puede desactivarla sin afectar al resto).
        host.subscribe(ON_CONVERSATION_STARTED, self._on_conversation_started)
        # Datos que el cliente dio en un recorrido de este pack (smoke de test 2026-10-20).
        host.subscribe(ON_PROCEDURE_SLOTS_SET, self._on_procedure_slots_set)

        # Coordinación con pack-payments SIN importarlo (invariante nº5, ADR-0023):
        # el nombre del evento es el contrato real, un string literal acordado en
        # esa ADR -- no una constante compartida entre packs.
        host.subscribe("payment.confirmed", self._on_payment_confirmed)
        host.subscribe("payment.rejected", self._on_payment_rejected)

        # Los `@register_cron` de `jobs.py` se registran al importar el módulo
        # (ver import a nivel de módulo arriba), no acá -- así el worker y el
        # beat también los registran al escanear el plugin.

    def _register_views(self) -> None:
        from core.admin.view_registry import (
            TableColumn,
            TableDescriptor,
            TableFilter,
            ViewDescriptor,
            view_registry,
        )

        view_registry.register(
            PLUGIN_ID,
            ViewDescriptor(
                title="Leads",
                path="leads",
                table=TableDescriptor(
                    columns=[
                        TableColumn(key="name", label="Lead"),
                        TableColumn(key="interest", label="Interés"),
                        TableColumn(key="segment", label="Clasificación"),
                        TableColumn(key="estado", label="Estado"),
                        TableColumn(key="pedidos", label="Pedidos"),
                        TableColumn(key="last_activity", label="Última actividad"),
                    ],
                    filters=[
                        TableFilter(
                            key="estado",
                            label="Estado",
                            options=[
                                {"value": "con_pedido", "label": "Con pedido"},
                                {"value": "escalada", "label": "Escalada"},
                                {"value": "con_cotizacion", "label": "Con cotización"},
                                {"value": "sin_actividad_comercial", "label": "Sin actividad"},
                            ],
                        ),
                        TableFilter(
                            key="segment",
                            label="Clasificación",
                            options=[
                                {"value": "nuevo", "label": "Nuevo"},
                                {"value": "ocasional", "label": "Ocasional"},
                                {"value": "frecuente", "label": "Frecuente"},
                                {"value": "fiel", "label": "Fiel"},
                                {"value": "inactivo", "label": "Inactivo"},
                            ],
                        ),
                    ],
                    page_size=20,
                ),
                form={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "title": "Nombre"},
                        "interest": {"type": "string", "title": "Interés"},
                    },
                },
            ),
            list_handler=_leads_list,
            save_handler=_leads_save,
        )

    # ------------------------------------------------------------ eventos
    async def _on_conversation_started(self, conversation_id: uuid.UUID, customer_id: uuid.UUID) -> None:
        """Crea un lead borrador cuando arranca una conversación nueva.

        Solo si el pack está habilitado para el tenant (ADR-0019) y su config
        tiene auto_create_lead activo (decisión por tenant, no global): sin esto,
        un tenant sin el pack o con la config apagada recibiría leads fantasma.
        """
        from core.agents.spec import enabled_packs, load_active_spec
        from core.plugin_manager import plugin_manager
        from core.shared.tenant import get_current_tenant_id

        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return
        spec = await load_active_spec(tenant_id)
        if self.id not in enabled_packs(spec):
            return
        config = await plugin_manager.load_config(self.id)
        if config is None or not getattr(config, "auto_create_lead", True):
            return

        from sqlalchemy import select

        from pack_commerce.models import Lead

        async with get_session() as session:
            existing = (
                await session.execute(
                    select(Lead).where(
                        Lead.customer_id == customer_id, Lead.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Lead(
                        customer_id=customer_id,
                        conversation_id=conversation_id,
                        status="nuevo",
                        idempotency_key=f"auto:{conversation_id}",
                    )
                )
                log.info("lead_draft_created", conversation_id=str(conversation_id))

    async def _on_procedure_slots_set(
        self,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        procedure_id: str,
        datos: dict[str, Any],
    ) -> None:
        """Completa el lead del cliente con los datos de contacto de un recorrido de ESTE pack
        (`contratar_plan`, `tomar_pedido`): con foco, `crear_lead`/`actualizar_lead` no se
        ofrecen. Solo si el pack está habilitado para el negocio; nunca crea un lead."""
        from core.agents.spec import enabled_packs, load_active_spec

        if not str(procedure_id).startswith(f"{self.id}.") or not isinstance(datos, dict):
            return
        actual = get_current_tenant_id()
        if actual is None or actual != tenant_id or not any(datos.get(n) for n in ("full_name", "email", "phone")):
            return
        from pack_commerce.skills import completar_lead

        try:
            if self.id not in enabled_packs(await load_active_spec(actual)):
                return
            async with get_session() as session:
                await completar_lead(
                    session,
                    customer_id,
                    nombre=datos.get("full_name"),
                    email=datos.get("email"),
                    telefono=datos.get("phone"),
                )
        except Exception as exc:  # el payload trae datos personales: el log solo lleva el tipo
            log.warning("lead_completar_fallo", error=type(exc).__name__)

    async def _on_payment_confirmed(
        self, reference: str, provider: str, provider_reference: str, amount: float
    ) -> None:
        await self._update_order_payment_status(reference, "pagado", amount)

    async def _on_payment_rejected(
        self, reference: str, provider: str, provider_reference: str, amount: float
    ) -> None:
        await self._update_order_payment_status(reference, "rechazado", amount)

    async def _update_order_payment_status(
        self, reference: str, payment_status: str, amount: float | None = None
    ) -> None:
        """Reacciona a eventos de `pack-payments` (ADR-0023) sin importarlo: por
        convención, `reference` es el `public_order_number` (o su versión `-rN`)
        que `confirmar_pago_pedido` le pasó al link. Si no matchea ninguna Order,
        se loggea y se ignora.

        Validaciones de HU-COM-01:
        - **monto**: un `paid` con monto distinto al total NO marca el pedido
          pagado (se alerta al negocio).
        - **rechazo tardío**: un `rejected` sobre un pedido ya pagado no cambia
          su estado.
        """
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return
        from core.agents.spec import enabled_packs, load_active_spec

        spec = await load_active_spec(tenant_id)
        if self.id not in enabled_packs(spec):
            return

        # La referencia puede venir versionada (`OC-0001-r2`): se normaliza a la
        # base para localizar el pedido.
        base_reference = reference.split("-r", 1)[0] if "-r" in reference else reference

        async with get_session() as session:
            order = (
                await session.execute(
                    select(Order).where(
                        Order.tenant_id == tenant_id,
                        Order.public_order_number == base_reference,
                    )
                )
            ).scalar_one_or_none()
            if order is None:
                log.warning("payment_event_no_matching_order", reference=reference, payment_status=payment_status)
                return
            # Rechazo tardío: un pedido ya pagado no vuelve atrás.
            if payment_status == "rechazado" and order.payment_status == "pagado":
                log.info("order_payment_rejected_after_paid", order_id=str(order.id))
                return
            # Monto: solo se marca pagado si coincide con el total del servidor.
            if payment_status == "pagado" and amount is not None:
                expected = round(float(order.total), 2)
                received = round(float(amount), 2)
                if abs(expected - received) > 0.01:
                    log.warning(
                        "order_payment_amount_mismatch",
                        order_id=str(order.id),
                        expected=expected,
                        received=received,
                    )
                    order.status_reason = (
                        f"pago con monto distinto al total ({received} != {expected})"
                    )
                    return
            ya_pagado = order.payment_status == "pagado"
            order.payment_status = payment_status
            order.status_changed_at = datetime.now(UTC)
            log.info("order_payment_status_updated", order_id=str(order.id), payment_status=payment_status)
            if payment_status == "pagado":
                await self._activate_plans_for_order(session, order)
                if not ya_pagado:
                    await self._emitir_pedido_pagado(session, tenant_id, order)

    @staticmethod
    async def _emitir_pedido_pagado(session, tenant_id, order: Order) -> None:  # type: ignore[no-untyped-def]
        """`order.paid` al outbox (ADR-0107) en la MISMA transacción que marca el pedido pagado,
        solo en esa transición y con el monto ya validado: cierra las tareas de `contratar_plan` y
        `tomar_pedido` que esperan el pago (HU-AOS-07), por el `order_id` que fijó `crear_pedido`.
        Idempotente por pedido (un webhook repetido o un reintento del despachador no duplica)."""
        from core.events.outbox import emit_domain
        from core.shared.events import ON_ORDER_PAID

        await emit_domain(
            session,
            tenant_id=tenant_id,
            topic=ON_ORDER_PAID,
            dedupe_key=f"{ON_ORDER_PAID}:{order.id}",
            payload={"order_id": str(order.id), "public_order_number": order.public_order_number},
        )

    async def _activate_plans_for_order(self, session, order: Order) -> None:
        """Plan como producto (ADR-0083, D-16): ver `pack_commerce.planes`."""
        from pack_commerce.planes import activar_planes_del_pedido

        await activar_planes_del_pedido(session, order)

    # ------------------------------------------------------------ verificador
    async def _afirmaciones(self) -> list[Afirmacion]:
        """Afirmaciones de cierre del pack para el verificador del core (ADR-0119):
        las incorporadas (`COMPLETION_CLAIMS`) menos las que este negocio desactivó
        (`completion_claims_disabled`), más las propias (`completion_claims_custom`,
        ADR-0025, siempre frases literales). Fail-open ante config ausente o
        inválida: quedan las incorporadas."""
        from core.plugin_manager import plugin_manager

        claims = dict(COMPLETION_CLAIMS)
        try:
            config = await plugin_manager.load_config(PLUGIN_ID)
        except Exception:
            log.warning("afirmaciones_config_fallo", plugin=PLUGIN_ID)
            config = None
        if config is not None:
            disabled = {
                n.strip() for n in (config.completion_claims_disabled or "").split(",") if n.strip()
            }
            for name in disabled:
                claims.pop(name, None)
            claims.update(_parse_custom_completion_claims(config.completion_claims_custom or ""))
        return [
            Afirmacion(
                label=label,
                patron=pattern,
                tools=frozenset(tools),
                basta_url=label in _LINK_CON_URL,
                # Solo la afirmación incorporada: una etiqueta que el negocio reescribe con frases
                # propias no hereda los datos (revisión de seguridad, 2026-10-18).
                datos=_DATOS_QUE_RESPALDAN.get(label, frozenset()) if pattern is COMPLETION_CLAIMS.get(label, (None,))[0] else frozenset(),
            )
            for label, (pattern, tools) in claims.items()
        ]