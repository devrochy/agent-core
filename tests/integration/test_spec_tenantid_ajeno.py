"""Un AgentSpec con `tenantId` ajeno no carga artefactos de otro negocio.

Hallazgo del auditor multi-tenant (2026-10-10): `PUT /api/v1/admin/settings/advanced/spec`
guardaba el `tenantId` del cuerpo, y el prompt resolvía `TENANTS_ROOT/<slug>/` desde ahí.
Ahora el `tenantId` se fuerza al negocio de la sesión al guardar, y los lectores sacan
el slug del `tenant_scope` (las filas viejas con un `tenantId` ajeno no cargan nada ajeno).
"""

from __future__ import annotations

import os
import shutil

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SECRETO = "Guion confidencial del negocio B."


async def _negocio(prefijo: str):
    from core.alta_negocio import onboard_tenant

    slug = f"{prefijo}-{os.urandom(3).hex()}"
    tid, _ = await onboard_tenant(
        name=f"Negocio {slug}", slug=slug, business_description="Test.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid, slug


async def _cookie(app_client, tenant_id) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303, r.text
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tenant_id)}
    )
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session}; {tenant_cookie}"


async def _fila_activa(tenant_id) -> dict:
    from sqlalchemy import select

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tenant_id,
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            return dict(row.spec)


def _override_de(slug: str):
    """Override de `tono` en `TENANTS_ROOT/<slug>/` con el secreto."""
    from core.onboarding import tenant_dir

    playbooks = tenant_dir(slug) / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    (playbooks / "tono.md").write_text(SECRETO, encoding="utf-8")
    return tenant_dir(slug)


async def _prompt(tenant_id) -> str:
    from core.agents.spec import load_active_spec
    from core.llm.prompt_builder import PromptContext, build_system_blocks
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        spec = await load_active_spec()
        blocks = await build_system_blocks(
            spec, PromptContext(assistant_name="A", business_description="B")
        )
    return "\n".join(blocks)


async def test_editor_crudo_ignora_tenantid_ajeno(app_client) -> None:
    a, _ = await _negocio("spec-a")
    b, slug_b = await _negocio("spec-b")
    carpeta_b = _override_de(slug_b)
    try:
        cookie = await _cookie(app_client, a)
        r = await app_client.get("/api/v1/admin/settings/advanced/spec", headers={"Cookie": cookie})
        assert r.status_code == 200, r.text
        spec = r.json()["spec"]
        assert any(p["name"] == "tono" for p in spec["playbooks"])
        spec["tenantId"] = str(b)
        spec["persona"]["maxLength"] = 321  # cambio real: fuerza versión nueva

        r2 = await app_client.put(
            "/api/v1/admin/settings/advanced/spec", headers={"Cookie": cookie}, json={"spec": spec}
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["spec"]["tenantId"] == str(a)

        fila = await _fila_activa(a)
        assert fila["tenantId"] == str(a)
        assert fila["persona"]["maxLength"] == 321
        assert SECRETO not in await _prompt(a)
        # Control: el dueño del override sí lo ve.
        assert SECRETO in await _prompt(b)
    finally:
        shutil.rmtree(carpeta_b, ignore_errors=True)


async def test_version_guardada_con_tenantid_ajeno_no_carga_lo_de_otro_negocio() -> None:
    """Filas escritas antes del arreglo (o un rollback a ellas) no cargan lo de otro negocio.

    El `tenantId` ajeno se conserva al cargar (el lector tolerante v3 lo reporta como
    aviso), pero ningún lector lo usa para resolver `TENANTS_ROOT/<slug>/`.
    """
    from sqlalchemy import select

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, _ = await _negocio("spec-a")
    b, slug_b = await _negocio("spec-b")
    carpeta_b = _override_de(slug_b)
    try:
        async with tenant_scope(a):
            async with get_session() as session:
                row = (
                    await session.execute(
                        select(AgentSpecRecord).where(
                            AgentSpecRecord.tenant_id == a,
                            AgentSpecRecord.is_active.is_(True),
                            AgentSpecRecord.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
                row.spec = {**row.spec, "tenantId": str(b)}
        assert SECRETO not in await _prompt(a)
    finally:
        shutil.rmtree(carpeta_b, ignore_errors=True)
