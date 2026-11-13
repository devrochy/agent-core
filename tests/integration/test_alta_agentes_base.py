"""ADR-0192 (HU-AOS-68) · El Coordinador nace con el negocio, sin General.

Contra el stack real, sin LLM ni red: el alta deja solo el Coordinador con su versión y el negocio en
v3; un negocio sin conexión no tiene especialista (D-35). Con la primera conexión conversacional el
outbox (`connection.release_1`) crea el especialista del preset y la release 1. Repetir el alta no
duplica nada.
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


@pytest.fixture(scope="module", autouse=True)
def _alta_con_agent_os():
    import _alta_agent_os

    _alta_agent_os.ACTIVO = True
    yield
    _alta_agent_os.ACTIVO = False


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


async def test_el_alta_crea_solo_el_coordinador_con_su_version() -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu68")
    tid, pasos = await onboard_tenant(name=f"HU68 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)

    assert await _agentes(tid) == {"coordinador": "core"}
    versiones = await _sql(tid, "select a.key, v.contract from agent_versions v join agents a on a.id = v.agent_id")
    assert versiones == [("coordinador", "kernelspec/1")]
    assert "Coordinador listo" in pasos
    assert not any("General" in p for p in pasos)


async def test_sin_agentes_base_el_alta_no_crea_agentes_ni_runtime() -> None:
    """Los journeys del gate montan su propio runtime: el alta con `agentes_base=False` los deja en paz."""
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu53-sin")
    tid, pasos = await onboard_tenant(
        name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, agentes_base=False
    )

    assert await _agentes(tid) == {}
    assert not any("Coordinador" in p or "runtime v3" in p for p in pasos)


async def test_repetir_el_alta_no_duplica_el_coordinador() -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu68")
    tid, _ = await onboard_tenant(name=f"HU68 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)
    await onboard_tenant(name=f"HU68 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, tenant_id=tid)

    assert await _agentes(tid) == {"coordinador": "core"}
    assert await _sql(tid, "select count(*) from agent_versions") == [(1,)]


# ------------------------------------------------------------------ alta del negocio nuevo (P-34)
async def test_el_alta_no_tiene_paso_de_runtime() -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu68")
    tid, pasos = await onboard_tenant(name=f"HU68 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)

    assert tid is not None
    assert not any(p.startswith("runtime v3") for p in pasos)


async def test_la_primera_conexion_trae_el_especialista_del_preset_y_su_release() -> None:
    """D-35: sin conexión no hay especialista; con la primera, el handler del outbox aplica los agentes
    del preset (el primero queda por defecto) y construye la release 1."""
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.events.core_handlers import on_connection_release_1
    from core.gateway import chat_web_conexion
    from core.shared.tenant import tenant_scope

    slug = uniq("hu68")
    tid, _ = await onboard_tenant(
        name=f"HU68 {slug}", slug=slug, preset="comercio", plan="premium", sembrar_ejemplos=False
    )
    assert await _agentes(tid) == {"coordinador": "core"}

    async with tenant_scope(tid):
        async with get_session() as session:
            await chat_web_conexion.guardar_widget(session, tid, enabled=False, allowed_origins=[])
            conexion = (await chat_web_conexion.conexion_del_negocio(session, tid)).id
    assert await _sql(tid, "select count(*) from domain_events where topic = 'connection.release_1'") == [(1,)]

    async with tenant_scope(tid):
        await on_connection_release_1(connection_id=str(conexion))
    instancias = await _sql(
        tid,
        "select a.key, i.role, i.is_default from agent_instances i join agents a on a.id = i.agent_id "
        "where i.connection_id = :c and i.deleted_at is null order by i.role, a.key",
        c=conexion,
    )
    assert instancias[0] == ("coordinador", "core", False)
    especialistas = [fila for fila in instancias if fila[1] == "specialist"]
    assert especialistas and "general" not in {k for k, _r, _d in especialistas}
    assert sum(1 for _k, _r, d in especialistas if d) == 1
    activa = await _sql(tid, "select active_release_id is not null from channel_connections where id = :c", c=conexion)
    assert activa == [(True,)]
