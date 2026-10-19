"""Adenda 2026-10-18 de ADR-0051: Configuración devuelve el aviso por defecto fuera de horario.

`identity.out_of_hours_default_message` es de solo lectura: sale del playbook del kernel con el
idioma y el nombre guardados, y mandarlo en el PUT no cambia nada.
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


async def test_identity_devuelve_el_aviso_por_defecto_con_el_nombre_guardado(app_client) -> None:
    from core.alta_negocio import onboard_tenant

    slug = f"fh-{os.urandom(3).hex()}"
    tid, _ = await onboard_tenant(
        name=f"FH {slug}", slug=slug, business_description="Test.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303, r.text
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tid)}
    )
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    cookie = f"{session}; {tenant_cookie}"

    payload = (await app_client.get("/api/v1/admin/settings", headers={"Cookie": cookie})).json()["identity"]
    payload.update(assistant_name="Lupe", out_of_hours_default_message="ignorado")
    r3 = await app_client.put("/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json=payload)
    assert r3.status_code == 200, r3.text
    esperado = (
        "¡Hola! Soy Lupe. Ahora estamos fuera de nuestro horario de atención. "
        "Te respondemos apenas volvamos."
    )
    assert r3.json()["out_of_hours_default_message"] == esperado

    current = (await app_client.get("/api/v1/admin/settings", headers={"Cookie": cookie})).json()["identity"]
    assert current["out_of_hours_default_message"] == esperado
    assert current["out_of_hours_message"] == "", "el aviso por defecto no se guarda como mensaje propio"
