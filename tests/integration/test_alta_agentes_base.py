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


async def test_sin_agentes_base_el_alta_no_crea_agentes_ni_runtime() -> None:
    """Los journeys del gate montan su propio runtime: el alta con `agentes_base=False` los deja en paz."""
    from core.alta_negocio import onboard_tenant

    slug = uniq("hu53-sin")
    tid, pasos = await onboard_tenant(
        name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, agentes_base=False
    )

    assert await _agentes(tid) == {}
    assert not any("General" in p or "runtime v3" in p for p in pasos)


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


# ------------------------------------------------------------------ runtime del negocio nuevo (P-34)
async def _runtime(tid: uuid.UUID) -> str:
    return (await _sql(tid, "select aos_runtime from tenants where id = :t", t=tid))[0][0]


async def test_con_el_ruteo_encendido_el_negocio_nuevo_nace_en_v3_y_queda_auditado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.alta_negocio import onboard_tenant
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    slug = uniq("hu53")
    tid, pasos = await onboard_tenant(name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)

    assert await _runtime(tid) == "v3"
    assert "runtime v3 encendido" in pasos
    # El recorte lo escribe el escritor auditado, no un UPDATE suelto.
    auditoria = await _sql(tid, "select actor from audit_log where action = 'agent_os_recorte_general'")
    assert [a for (a,) in auditoria] == ["alta"]


async def test_con_el_ruteo_apagado_el_negocio_nuevo_queda_en_legacy_y_lo_avisa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.alta_negocio import onboard_tenant
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", False)
    slug = uniq("hu53")
    tid, pasos = await onboard_tenant(name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)

    assert await _runtime(tid) == "legacy"
    assert any(p.startswith("runtime v3 pendiente") for p in pasos)
    assert await _agentes(tid) == {"coordinador": "core", "general": "specialist"}  # los agentes igual nacen


async def test_fijar_bandera_audita_solo_si_cambia() -> None:
    from core.agents import coordinacion
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("hu53")
    tid, _ = await onboard_tenant(name=f"HU53 {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False)
    async with tenant_scope(tid):
        assert await coordinacion.estado_de_bandera(tid) is True
        assert await coordinacion.fijar_bandera(tid, True, actor="cli") is False  # ya estaba
        assert await coordinacion.fijar_bandera(tid, False, actor="cli:test") is True
        assert await coordinacion.estado_de_bandera(tid) is False
    filas = await _sql(tid, "select actor from audit_log where action = 'agent_os_recorte_general' order by created_at")
    assert [a for (a,) in filas] == ["alta", "cli:test"]
    # No pisa el resto de settings.agent_os.
    assert (await _sql(tid, "select settings->>'language' from tenants where id = :t", t=tid))[0][0] == "es"
