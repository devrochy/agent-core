"""Editor de AgentSpec del panel (C4, Sprint 4): guardar crea una versión nueva
y la activa; un valor fuera del contrato se rechaza sin tocar la BD.

Opera sobre un tenant de ejemplo genérico para no ensuciar el spec de t1.
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


async def _cookies(app_client, tenant_id) -> tuple[str, str]:
    """Login + switch a un tenant. Devuelve (session_cookie, ac_tenant_cookie)."""
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    session = r.headers["set-cookie"].split(";")[0]
    headers = {"Cookie": session}
    r2 = await app_client.post(
        "/admin/switch-tenant", headers=headers, data={"tenant_id": str(tenant_id)}
    )
    tenant_cookie = [c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c]
    return session, tenant_cookie[0]


def _payload(**overrides) -> dict[str, str]:
    base = {
        "packs": "pack_commerce",
        "persona_role": "asistente de ventas",
        "persona_tone": "cercano",
        "persona_warmth": "3",
        "persona_emojis": "sparse",
        "persona_max_length": "500",
        "model_primary": "deepseek-chat",
        "model_temperature": "0.3",
        "policy_budget": "0.5",
        "policy_max_tool_calls": "10",
    }
    base.update(overrides)
    return base


async def _active_versions(tenant_id) -> list[str]:
    from sqlalchemy import select

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
    return [r.version for r in rows if r.is_active]


async def test_editor_crea_version_nueva_y_activa(app_client, moto_setup) -> None:
    _t1, t2 = moto_setup
    session, ac_tenant = await _cookies(app_client, t2)
    headers = {"Cookie": f"{session}; {ac_tenant}"}

    # El editor vive ahora en la tab "Avanzado" de Configuración (ADR-0050); la
    # ruta /admin/spec redirige allí.
    r = await app_client.get("/admin/spec", headers=headers, follow_redirects=False)
    assert r.status_code == 307
    assert "tab=avanzado" in r.headers["location"]

    r = await app_client.get("/admin/configuracion?tab=avanzado", headers=headers)
    assert r.status_code == 200
    assert "AgentSpec" in r.text
    assert "Versiones del AgentSpec" in r.text

    antes = await _active_versions(t2)
    r = await app_client.post("/admin/spec", headers=headers, data=_payload())
    assert r.status_code == 303
    assert "activado" in r.headers["location"]
    despues = await _active_versions(t2)
    assert despues and despues != antes, "debe quedar una versión nueva activa"


async def test_editor_rechaza_valor_fuera_del_contrato(app_client, moto_setup) -> None:
    """El contrato congelado es rey: un tono inválido no guarda ninguna versión."""
    _t1, t2 = moto_setup
    session, ac_tenant = await _cookies(app_client, t2)
    headers = {"Cookie": f"{session}; {ac_tenant}"}

    antes = await _active_versions(t2)
    r = await app_client.post(
        "/admin/spec", headers=headers, data=_payload(persona_tone="amable")
    )
    assert r.status_code == 303
    assert "spec_invalido" in r.headers["location"], "tono inválido → flash de error"
    assert (await _active_versions(t2)) == antes, "no debe crearse versión con un spec inválido"