"""HU-AOS-53 · El General y el Coordinador nacen con el negocio (SPEC-39).

Contra el stack real, sin LLM ni red: el alta deja los dos agentes reservados con su versión, el
negocio sigue en `aos_runtime = legacy` (no cambia cómo atiende) y el recorte del General queda
encendido solo en un negocio NUEVO. Repetir el alta no duplica nada ni pisa la bandera del master.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _agentes(tid: uuid.UUID) -> dict[str, str]:
    filas = await _sql(tid, "select key, kind from agents where deleted_at is null order by key")
    return {k: kind for k, kind in filas}


async def test_el_alta_crea_general_y_coordinador_con_su_version() -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu53")
    tid, pasos = await onboard_tenant(name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)

    assert await _agentes(tid) == {"coordinador": "core", "general": "specialist"}
    versiones = await _sql(
        tid, "select a.key from agent_versions v join agents a on a.id = v.agent_id order by a.key"
    )
    assert [k for (k,) in versiones] == ["coordinador", "general"]
    assert "agentes General y Coordinador listos" in pasos
    assert "recorte del General encendido" in pasos


async def test_el_negocio_nuevo_enciende_el_recorte_y_sigue_en_legacy() -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu53")
    tid, _ = await onboard_tenant(name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)

    ajustes = (await _sql(tid, "select settings->'agent_os'->>'recortar_general', settings->'agent_os'->>'runtime' "
                               "from tenants where id = :t", t=tid))[0]
    assert ajustes[0] == "true"
    # No se migra el runtime en el alta (P-34): el negocio atiende como antes.
    assert ajustes[1] in (None, "legacy")


async def test_repetir_el_alta_no_duplica_ni_pisa_la_bandera_del_master() -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu53")
    tid, _ = await onboard_tenant(name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)
    await _sql(
        tid,
        "update tenants set settings = jsonb_set(settings, '{agent_os,recortar_general}', 'false'::jsonb) "
        "where id = :t",
        t=tid,
    )

    _, pasos = await onboard_tenant(
        name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, tenant_id=tid
    )

    assert await _agentes(tid) == {"coordinador": "core", "general": "specialist"}
    assert "recorte del General encendido" not in pasos
    bandera = (await _sql(tid, "select settings->'agent_os'->>'recortar_general' from tenants where id = :t", t=tid))[0][0]
    assert bandera == "false"
