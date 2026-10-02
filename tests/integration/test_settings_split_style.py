"""HU-ARCH-11: un solo mensaje por respuesta por defecto y estilo editable.

- El default del sistema es `un_mensaje`.
- `/api/v1/settings/identity` guarda y devuelve `split_style`.
- Un valor inválido cae a `un_mensaje` (nunca deja el sistema sin estilo).
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


def test_default_global_es_un_mensaje(monkeypatch) -> None:
    for var in ("MESSAGE_SPLIT_STYLE",):
        monkeypatch.delenv(var, raising=False)
    from core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.message_split_style == "un_mensaje"


async def test_identity_guarda_y_devuelve_split_style(app_client) -> None:
    """PUT /settings/identity persiste split_style; GET lo devuelve."""
    from core.onboarding import onboard_tenant

    slug = f"split-{os.urandom(3).hex()}"
    tid, _ = await onboard_tenant(
        name=f"Split {slug}", slug=slug, business_description="Test.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )

    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    if r.status_code != 303:
        pytest.skip("login admin no disponible en este entorno")
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tid)}
    )
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    cookie = f"{session}; {tenant_cookie}"

    # GET inicial: deriva `un_mensaje` (sin clave explícita).
    current = (await app_client.get("/api/v1/settings/identity", headers={"Cookie": cookie})).json()
    assert current["split_style"] == "un_mensaje"

    # PUT con `varios_cortos`.
    payload = dict(current)
    payload["split_style"] = "varios_cortos"
    r3 = await app_client.put(
        "/api/v1/settings/identity", headers={"Cookie": cookie}, json=payload
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["split_style"] == "varios_cortos"

    # PUT con un valor inválido: cae a `un_mensaje`.
    payload["split_style"] = "inventado"
    r4 = await app_client.put(
        "/api/v1/settings/identity", headers={"Cookie": cookie}, json=payload
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["split_style"] == "un_mensaje"
