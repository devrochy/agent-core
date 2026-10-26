"""Toda tabla de `public` con `tenant_id` tiene RLS habilitada y forzada, salvo las de sistema
justificadas (auditoría multitenant del 2026-10-26).

`scripts/verificar_aislamiento.py` y `check_rls_forced` solo detectan «RLS habilitada sin FORCE»:
una tabla con `tenant_id` y sin RLS pasaba en silencio. Esta prueba compara contra una lista
explícita. Si falla porque aparece una tabla NUEVA, lo normal es habilitarle y forzarle RLS con la
policy de aislamiento (`/db-migration`); solo si se lee antes de tener un negocio en contexto se
suma aquí y a `docs/MULTITENANT.md` (§ «Tablas de sistema»), con la marca «Tabla de SISTEMA» en la
migración.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Tablas de sistema con columna `tenant_id` y sin RLS: por qué, en docs/MULTITENANT.md.
SIN_RLS_POR_DISENO = {
    "admin_permissions",  # ADR-0047: `tenant_id` nulo = permiso global; RLS fallaría cerrado
    "qa_tester_datasets",  # ADR-0135 §4: datos de prueba por tester, solo el master
    "tenant_invitations",  # ADR-0081: el invitado la ve antes de ser miembro
    "tenant_user",  # ADR-0012: membresía, se consulta antes de conocer el negocio
    "webhook_events",  # ADR-0028: el webhook llega antes de resolver el negocio
}


async def test_las_tablas_con_tenant_id_sin_rls_forzada_son_las_de_sistema_documentadas() -> None:
    from core.db.session import get_session

    async with get_session() as s:
        filas = (
            await s.execute(
                text(
                    "select c.relname from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "join pg_attribute a on a.attrelid = c.oid and a.attname = 'tenant_id' and not a.attisdropped "
                    "where n.nspname = 'public' and c.relkind in ('r', 'p') "
                    "and not (c.relrowsecurity and c.relforcerowsecurity)"
                )
            )
        ).all()
    sin_rls = {f[0] for f in filas}
    assert sin_rls == SIN_RLS_POR_DISENO, (
        f"nuevas sin RLS forzada: {sorted(sin_rls - SIN_RLS_POR_DISENO)}; "
        f"ya la tienen y sobran en la lista: {sorted(SIN_RLS_POR_DISENO - sin_rls)}"
    )
