"""crm_0009 (HU-COM-03): el backfill del número público no trunca.

`lpad(x, 4)` TRUNCA en Postgres: la columna computada vieja mostraba `OC-8379` para
los pedidos 83790..83799 y repetir ese cálculo en el backfill dejaba duplicados que
hacían abortar el índice único (visto en la base local el 2026-10-08).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

MIGRACION = (
    Path(__file__).resolve().parents[2]
    / "packages/pack-commerce/alembic/versions/0009_orders_numero_publico.py"
)


def _expresion() -> str:
    spec = importlib.util.spec_from_file_location("crm_0009", MIGRACION)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo.BACKFILL_NUMERO


@pytest.mark.asyncio
async def test_backfill_rellena_con_ceros_pero_nunca_trunca() -> None:
    from core.db.session import get_session

    expr = _expresion()
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    f"select order_seq, {expr} from (values (7), (9999), (10000), (83790), (83799), (837966))"
                    " as t(order_seq) order by order_seq"
                )
            )
        ).all()
    assert [numero for _, numero in filas] == [
        "OC-0007", "OC-9999", "OC-10000", "OC-83790", "OC-83799", "OC-837966",
    ]
    assert len({numero for _, numero in filas}) == len(filas)
