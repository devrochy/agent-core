"""HU-CFG-02 (SPEC-37): `GET /api/v1/admin/settings/catalog`.

Gherkin: catálogo completo para el master; sin las `waweb_*` para un colaborador con la sección
de Configuración; 403 sin esa sección; guardrails apagables distintos por negocio.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import api_headers, uniq

from core.config_registro.opciones import OPCIONES

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/settings/catalog"
PASSWORD = "Colab-Catalogo-2026!"


async def _colaborador(tid, *, acceso: str) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        # Fila explícita sobre Configuración (lo que el dueño ve en Colaboradores).
        session.add(AdminPermission(admin_id=admin.id, section="settings", access=acceso, tenant_id=tid))
    return email


async def _negocio(*, packs: list[str] | None = None):
    from core.alta_negocio import onboard_tenant

    slug = uniq("cfg02")
    tid, _ = await onboard_tenant(
        name=f"N {slug}", slug=slug, persona={"tone": "cercano"}, packs=packs, admin_email="admin@local"
    )
    return tid


def _claves(cuerpo: dict) -> list[str]:
    return [o["clave"] for o in cuerpo["opciones"]]


async def test_el_master_recibe_todas_las_opciones_con_su_forma(app_client) -> None:
    tid = await _negocio()
    r = await app_client.get(URL, headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    # Todas las del registro: el conteo sale de él, no de un literal que se rompe con cada opción nueva.
    assert len(cuerpo["opciones"]) == len(OPCIONES)
    assert cuerpo["version"]
    velocidad = next(o for o in cuerpo["opciones"] if o["clave"] == "debounce_mode")
    assert velocidad["etiqueta"] == "Velocidad de respuesta"
    assert velocidad["tipo"] == "elegir_una"
    assert {"valor": "corto", "etiqueta": "Corta", "nota": "espera 3 s"} in velocidad["valores"]
    assert velocidad["editable_por"] == "dueno" and velocidad["almacen"] == "behavior_config:debounce_mode"
    umbral = next(o for o in cuerpo["opciones"] if o["clave"] == "difficulty_score_threshold")
    assert (umbral["minimo"], umbral["maximo"]) == (1, 10)


async def test_un_colaborador_con_configuracion_no_ve_la_politica_de_whatsapp_web(app_client) -> None:
    tid = await _negocio()
    email = await _colaborador(tid, acceso="read")
    r = await app_client.get(URL, headers=await api_headers(app_client, tid, email=email, password=PASSWORD))
    assert r.status_code == 200, r.text
    claves = _claves(r.json())
    # Las que no son exclusivas del master (las `waweb_*` lo son, y se comprueba aparte).
    assert len(claves) == len([o for o in OPCIONES if o.editable_por != "master"])
    assert not [c for c in claves if c.startswith("waweb_")]


async def test_sin_la_seccion_de_configuracion_responde_403(app_client) -> None:
    tid = await _negocio()
    email = await _colaborador(tid, acceso="none")
    r = await app_client.get(URL, headers=await api_headers(app_client, tid, email=email, password=PASSWORD))
    assert r.status_code == 403, r.text


async def test_los_guardrails_apagables_dependen_de_los_packs_del_negocio(app_client) -> None:
    from core.mcp.host import mcp_host
    from core.orchestrator.verificador import TOGGLES

    con_comercio = await _negocio(packs=["pack_commerce"])
    sin_packs = await _negocio(packs=[])

    async def apagables(tid) -> set[str]:
        r = await app_client.get(URL, headers=await api_headers(app_client, tid))
        assert r.status_code == 200, r.text
        opcion = next(o for o in r.json()["opciones"] if o["clave"] == "disabled_guardrails")
        return {v["valor"] for v in opcion["valores"]}

    solo_core = await apagables(sin_packs)
    con_pack = await apagables(con_comercio)
    del_pack = {g.name for g in mcp_host.get_guardrails() if g.plugin_id == "pack_commerce"}

    assert set(TOGGLES) <= solo_core
    assert solo_core.isdisjoint(del_pack - set(TOGGLES))
    assert con_pack == solo_core | del_pack
