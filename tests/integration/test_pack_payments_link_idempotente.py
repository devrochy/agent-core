"""`generar_link_pago` debe ser idempotente por `reference`, no solo por
`idempotency_key` (que solo cubre el reintento del MISMO tool_call).

Hueco real encontrado (2026-09-12), del mismo tipo ya corregido este mes
en `pack_commerce` (`_parse_uuid`, items no-dict, etc.): la tabla
`pack_payments_transactions` tiene un índice único real
`(tenant_id, reference)` (`pack_payments/models.py:49`), pero
`generar_link_pago` no lo verificaba antes de insertar. Si el modelo
llama la tool dos veces para el MISMO pedido en turnos distintos (dos
tool_calls, dos `idempotency_key` diferentes), la segunda reventaba con
`IntegrityError` -- no capturada, envuelta en `PluginError` por
`core/mcp/host.py::execute_tool`, que dispara `auto_disable_for_tenant`
(ADR-0024) y deja al cliente sin respuesta clara.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID, TEST_BETA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID
T2 = TEST_BETA_ID


class _FakeHttp:
    """`context.http` simulado: Wompi siempre responde un link (no sale a red)."""

    async def request(self, *a, **k):  # type: ignore[no-untyped-def]
        return {"status": 200, "data": {"data": {"id": "link-fake-test"}}}


async def _tool_context():
    from core.mcp.types import ToolContext

    # La tool sale a Wompi solo por `context.http` (ADR-0052).
    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
        http=_FakeHttp(),
    )


async def _save_test_config() -> None:
    """Debe llamarse dentro de un `tenant_scope` -- `save_config` guarda
    la config para el tenant actual del contexto."""
    from core.plugin_manager import plugin_manager

    await plugin_manager.save_config(
        "pack_payments",
        {
            "public_key": "pub_test_x",
            "private_key": "prv_test_x",
            "events_secret": "events_secret_x",
            "sandbox": True,
        },
    )


async def test_generar_link_pago_es_idempotente_por_reference():
    from pack_payments.skills import PaymentSkill

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    # `save_config` exige que el plugin esté en `plugin_manager._modules`
    # (poblado por `scan()`, normalmente disparado por el arranque de la app
    # -- `core/main.py:71` -- que este archivo no importa). `scan()` es
    # idempotente, seguro llamarlo de nuevo si ya corrió.
    plugin_manager.scan()

    async with tenant_scope(T1):
        await _save_test_config()

        skill = PaymentSkill()
        reference = f"OC-{uuid.uuid4().hex[:8]}"

        ctx1 = await _tool_context()
        result1 = await skill.generar_link_pago(
            {"reference": reference, "amount": 200_000}, ctx1
        )
        assert "error" not in result1, result1

        # Mismo reference, tool_call DISTINTO (otro turno) -- antes del fix,
        # esto reventaba con IntegrityError contra el índice único.
        ctx2 = await _tool_context()
        result2 = await skill.generar_link_pago(
            {"reference": reference, "amount": 200_000}, ctx2
        )
        assert "error" not in result2, result2
        assert result2["_safe_append"] == result1["_safe_append"]

    from pack_payments.models import PaymentTransaction
    from sqlalchemy import select

    from core.db.session import get_session

    async with tenant_scope(T1):
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(PaymentTransaction).where(PaymentTransaction.reference == reference)
                )
            ).scalars().all()
    assert len(rows) == 1, "no debe crear una segunda transacción para el mismo reference"

    # Limpieza: no dejar la fila real en la BD compartida de tests.
    async with tenant_scope(T1):
        async with get_session() as session:
            await session.execute(
                PaymentTransaction.__table__.delete().where(PaymentTransaction.reference == reference)
            )


async def test_generar_link_pago_mismo_reference_no_cruza_tenant():
    """Hallazgo de `auditor-multitenant` (2026-09-12): el índice único real
    es `(tenant_id, reference)`, no solo `reference` -- dos tenants pueden
    usar el MISMO valor de referencia (ej. ambos numeran sus pedidos
    "OC-0001") sin pisarse ni ver la transacción del otro. Esta es la
    prueba de regresión que faltaba para ese caso."""
    from pack_payments.models import PaymentTransaction
    from pack_payments.skills import PaymentSkill
    from sqlalchemy import select

    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()

    reference = f"OC-{uuid.uuid4().hex[:8]}"

    async with tenant_scope(T1):
        await _save_test_config()
        result_t1 = await PaymentSkill().generar_link_pago(
            {"reference": reference, "amount": 200_000}, await _tool_context()
        )
        assert "error" not in result_t1, result_t1

    async with tenant_scope(T2):
        await _save_test_config()
        result_t2 = await PaymentSkill().generar_link_pago(
            {"reference": reference, "amount": 300_000}, await _tool_context()
        )
        assert "error" not in result_t2, result_t2

    # Cada tenant debe ver únicamente su propia transacción -- ambas con
    # el mismo `reference`, sin `IntegrityError` (el índice es compuesto)
    # y sin que la respuesta de uno filtre el monto del otro (el fake de
    # Wompi devuelve siempre el mismo link, por eso se compara `amount`,
    # que sí viene de la fila propia de cada tenant).
    assert result_t1["amount"] == 200_000.0
    assert result_t2["amount"] == 300_000.0

    async with tenant_scope(T1):
        async with get_session() as session:
            rows_t1 = (
                await session.execute(
                    select(PaymentTransaction).where(PaymentTransaction.reference == reference)
                )
            ).scalars().all()
    async with tenant_scope(T2):
        async with get_session() as session:
            rows_t2 = (
                await session.execute(
                    select(PaymentTransaction).where(PaymentTransaction.reference == reference)
                )
            ).scalars().all()

    assert len(rows_t1) == 1
    assert len(rows_t2) == 1
    assert float(rows_t1[0].amount) == 200_000.0
    assert float(rows_t2[0].amount) == 300_000.0

    # Limpieza.
    async with tenant_scope(T1):
        async with get_session() as session:
            await session.execute(
                PaymentTransaction.__table__.delete().where(PaymentTransaction.reference == reference)
            )
    async with tenant_scope(T2):
        async with get_session() as session:
            await session.execute(
                PaymentTransaction.__table__.delete().where(PaymentTransaction.reference == reference)
            )
