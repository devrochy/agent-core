"""Editor del AgentSpec v2 en el panel: retirado (ADR-0206, HU-AOS-91).

El turno no lee el AgentSpec v2 desde HU-AOS-90. Guardarlo desde el SSR (`POST /admin/spec`) o desde la API
(`/api/v1/admin/settings/advanced/spec`, versiones, guardrails, tools del negocio y declarativas) responde 410
`v2_retirado`, sin tocar la BD.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import api_headers

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _versiones(tenant_id) -> int:  # type: ignore[no-untyped-def]
    from sqlalchemy import func, select

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            return int(
                (
                    await s.execute(
                        select(func.count()).select_from(AgentSpecRecord).where(AgentSpecRecord.tenant_id == tenant_id)
                    )
                ).scalar_one()
            )


async def test_el_editor_ssr_del_spec_esta_retirado(app_client, moto_setup) -> None:  # type: ignore[no-untyped-def]
    _t1, moto = moto_setup
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    sesion = r.headers["set-cookie"].split(";")[0]
    r = await app_client.post("/admin/switch-tenant", headers={"Cookie": sesion}, data={"tenant_id": str(moto)})
    tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    antes = await _versiones(moto)

    r = await app_client.post("/admin/spec", headers={"Cookie": f"{sesion}; {tenant}"}, data={"raw_json": "{}"})
    assert r.status_code == 410
    assert await _versiones(moto) == antes


async def test_la_api_del_spec_v2_responde_410(app_client, moto_setup) -> None:  # type: ignore[no-untyped-def]
    _t1, moto = moto_setup
    h = await api_headers(app_client, moto)
    antes = await _versiones(moto)
    base = "/api/v1/admin/settings"
    llamadas = [
        ("GET", f"{base}/advanced/spec", None),
        ("PUT", f"{base}/advanced/spec", {"spec": {}}),
        ("POST", f"{base}/advanced/versions/x/activate", None),
        ("PUT", f"{base}/advanced/guardrails", {"guardrails_text": "max_length | 300"}),
        ("PUT", f"{base}/skills", {"tools": []}),
        ("GET", f"{base}/declarative-tools", None),
        ("PUT", f"{base}/declarative-tools", {"tools": [], "secrets": {}}),
    ]
    for metodo, url, cuerpo in llamadas:
        r = await app_client.request(metodo, url, headers=h, json=cuerpo)
        assert r.status_code == 410, (metodo, url, r.text)
        assert r.json()["error"]["code"] == "v2_retirado"
    assert await _versiones(moto) == antes

    # Lo que no era del spec sigue: el LLM propio, el flujo y las corridas de evals; lo del v2 viene vacío.
    avanzado = (await app_client.get(f"{base}/advanced", headers=h)).json()
    assert avanzado["versions"] == [] and avanzado["guardrails_text"] == "" and avanzado["spec"] == {}
    assert "llm" in avanzado and "flow" in avanzado and "eval_runs" in avanzado
    assert (await app_client.get(base, headers=h)).json()["skills"] == []
