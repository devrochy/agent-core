"""Guarda de integración R1: toda tabla de negocio en `public` con RLS debe FORZARLA.

Detecta N-36 (pack_subscriptions sin FORCE). Reutiliza `check_rls_forced()`, el
mismo chequeo del checklist de producción, así corre igual en local y en el CI.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_toda_tabla_con_rls_esta_forzada() -> None:
    from core.gateway.meta_production_readiness import check_rls_forced

    check = await check_rls_forced()
    assert check.ok, (
        "Hay tablas con RLS sin FORCE (el dueño de la tabla se salta la policy). "
        f"Detalle: {check.detail}"
    )
