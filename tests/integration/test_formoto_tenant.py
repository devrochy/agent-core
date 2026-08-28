"""Aislamiento de FORMOTO (tenant #2, Sprint 4 Fase C) contra el stack real.

FORMOTO corre sobre pack-commerce: catálogo de 100 productos (COP), enablement
por tenant y numeración de pedidos independiente. No hay fuga con CULTO.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_formoto_catalogo_aislado(formoto_setup) -> None:
    t1, t2 = formoto_setup
    from pack_commerce.models import Product

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        async with get_session() as session:
            n1 = (await session.execute(select(func.count()).select_from(Product))).scalar()
    async with tenant_scope(t2):
        async with get_session() as session:
            n2 = (await session.execute(select(func.count()).select_from(Product))).scalar()
    assert n1 == 4, "CULTO mantiene su catálogo (4)"
    assert n2 == 100, "FORMOTO tiene su catálogo (100)"


async def test_formoto_spec_enablement_y_escalacion(formoto_setup) -> None:
    t1, t2 = formoto_setup
    from core.agents.spec import enabled_packs, load_active_spec
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t2):
        spec = await load_active_spec()
    assert "pack_commerce" in enabled_packs(spec), "FORMOTO tiene el pack habilitado"
    assert "compatibilidad" in spec.policies.escalation.triggers, (
        "la compatibilidad técnica escala a humano (patrón de agent-sale)"
    )

    # CULTO no debe haberse contagiado el trigger
    async with tenant_scope(t1):
        spec_culto = await load_active_spec()
    assert "compatibilidad" not in spec_culto.policies.escalation.triggers


async def test_formoto_numeracion_independiente(formoto_setup) -> None:
    """ADR-0018: CULTO y FORMOTO numeran OC-0001 sin colisión (contador por tenant)."""
    t1, t2 = formoto_setup
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    for tid in (t1, t2):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("delete from pack_commerce_order_counters where tenant_id = :t"),
                    {"t": tid},
                )
    async with tenant_scope(t1):
        async with get_session() as session:
            s1 = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t1})).scalar()
    async with tenant_scope(t2):
        async with get_session() as session:
            s2 = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t2})).scalar()
    assert s1 == 1 and s2 == 1, "ambos tenants arrancan su numeración en 1, de forma independiente"