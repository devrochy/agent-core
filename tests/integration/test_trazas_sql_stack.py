"""Spans de SQL contra el Postgres real (asyncpg) sin los valores de los parámetros (B2, ADR-0188)."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SECRETO = "+573001234567"


async def test_db_statement_con_marcadores_y_sin_valores():
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings
    from core.observability.tracing import instrumentar_engine

    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    engine = create_async_engine(get_settings().database_url)
    instrumentar_engine(engine, tracer_provider=proveedor)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT CAST(:telefono AS text)"), {"telefono": SECRETO})
    finally:
        SQLAlchemyInstrumentor().uninstrument()
        await engine.dispose()

    spans = exportador.get_finished_spans()
    statements = [str(s.attributes.get("db.statement", "")) for s in spans]
    assert any("$1" in st for st in statements), statements
    assert SECRETO not in repr([dict(s.attributes or {}) for s in spans])
