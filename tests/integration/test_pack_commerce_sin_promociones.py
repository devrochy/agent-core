"""El agente no anuncia que no hay promociones (pedido del dueño, 2026-11-07).

`aplicar_promocion` es el único camino por el que el modelo se entera de las promociones. Cuando no
hay ninguna vigente (o ninguna aplica a la cotización) devuelve datos, no una frase: una oración
como «No hay promociones vigentes» invitaba al modelo a repetírsela al cliente. La regla de
comportamiento vive en el playbook `ventas` (ver `tests/test_playbook_ventas_promociones.py`).
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from _api_helpers import ADMIN_EMAIL, uniq

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _negocio_con_cotizacion() -> tuple[uuid.UUID, uuid.UUID, str]:
    """Un negocio propio (sin promociones de otros tests) con una cotización de 20.000."""
    from pack_commerce.models import Inventory, Product, ProductVariant, Quote, QuoteItem

    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("sinpromo")
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Test de promociones.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    customer_id = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            product = Product(tenant_id=tid, name="Café")
            session.add(product)
            await session.flush()
            variant = ProductVariant(tenant_id=tid, product_id=product.id, sku=f"SKU-{slug}", price=10_000)
            session.add(variant)
            await session.flush()
            session.add(Inventory(tenant_id=tid, variant_id=variant.id, stock_quantity=50))
            quote = Quote(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=customer_id,
                subtotal=20_000, discount=0, total=20_000, status="draft",
            )
            session.add(quote)
            await session.flush()
            session.add(
                QuoteItem(tenant_id=tid, quote_id=quote.id, variant_id=variant.id, quantity=2, unit_price=10_000)
            )
            return tid, customer_id, str(quote.id)


def _ctx(customer_id: uuid.UUID):  # type: ignore[no-untyped-def]
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


def _sin_frases(result: dict) -> None:
    """Nada en el resultado habla de promociones como texto: solo datos."""
    assert "message" not in result
    assert not [v for v in result.values() if isinstance(v, str) and "promoci" in v.lower()], result


async def test_sin_promociones_vigentes_devuelve_datos_y_no_una_frase() -> None:
    from pack_commerce.models import Promotion, utcnow
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, customer_id, quote_id = await _negocio_con_cotizacion()
    ahora = utcnow()
    async with tenant_scope(tid):
        # Ninguna cuenta como vigente: apagada, vencida y todavía no empieza.
        async with get_session() as session:
            regla = {"kind": "temporada", "discount_pct": 50}
            session.add(Promotion(tenant_id=tid, type="temporada", rules=regla, active=False))
            session.add(Promotion(tenant_id=tid, type="temporada", rules=regla, valid_to=ahora - timedelta(days=1)))
            session.add(Promotion(tenant_id=tid, type="temporada", rules=regla, valid_from=ahora + timedelta(days=1)))
        result = await OrderSkill().aplicar_promocion({"quote_id": quote_id}, _ctx(customer_id))

    assert result == {
        "quote_id": quote_id,
        "applied": False,
        "motivo": "sin_vigentes",
        "subtotal": 20_000.0,
        "total": 20_000.0,
    }
    _sin_frases(result)


async def test_promocion_vigente_que_no_aplica_tampoco_devuelve_una_frase() -> None:
    from pack_commerce.models import Promotion
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, customer_id, quote_id = await _negocio_con_cotizacion()
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                Promotion(
                    tenant_id=tid, type="temporada", rules={"kind": "temporada", "discount_pct": 50},
                    active=True, eligible_segments=["segmento-que-este-cliente-no-tiene"],
                )
            )
        result = await OrderSkill().aplicar_promocion({"quote_id": quote_id}, _ctx(customer_id))

    assert result["applied"] is False
    assert result["motivo"] == "no_aplica"
    assert result["total"] == 20_000.0
    _sin_frases(result)


async def test_con_promocion_aplicable_si_la_devuelve() -> None:
    """Control: cuando hay una vigente y aplica, el resultado la lleva (es lo único que el modelo puede anunciar)."""
    from pack_commerce.models import Promotion
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, customer_id, quote_id = await _negocio_con_cotizacion()
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                Promotion(
                    tenant_id=tid, type="temporada", active=True,
                    rules={"kind": "temporada", "discount_pct": 10, "label": "10 % de temporada"},
                )
            )
        result = await OrderSkill().aplicar_promocion({"quote_id": quote_id}, _ctx(customer_id))

    assert result["applied"] is True
    assert result["label"] == "10 % de temporada"
    assert result["discount"] == 2_000.0
    assert result["total"] == 18_000.0
