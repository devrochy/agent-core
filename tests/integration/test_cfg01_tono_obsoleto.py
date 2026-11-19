"""HU-CFG-01 (SPEC-37): `behavior_config["tono"] ` se acepta y se ignora; nadie lo escribe.

Gherkin: APK vieja que aún envía `tono`; altas sin `tono`; lectura compatible.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _behavior(tid) -> dict:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        fila = (await s.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
        return dict(fila.behavior_config or {})


async def _sesion(app_client, tid) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303, r.text
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post("/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tid)})
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session}; {tenant_cookie}"


async def test_alta_con_y_sin_preset_no_escribe_tono() -> None:
    """Gherkin «Altas sin `tono`»: el alta directa y el alta con preset dejan `behavior_config` sin la clave."""
    from core.alta_negocio import onboard_tenant

    slug = uniq("cfg01")
    tid, _ = await onboard_tenant(name=f"N {slug}", slug=slug, persona={"tone": "cercano"})
    assert "tono" not in await _behavior(tid)

    slug2 = uniq("cfg01p")
    tid2, _ = await onboard_tenant(name=f"N {slug2}", slug=slug2, preset="restaurante", plan="basic")
    assert "tono" not in await _behavior(tid2)


async def test_apk_vieja_envia_tono_y_no_se_escribe(app_client) -> None:
    """Gherkin «APK vieja»: el PUT con `tono` responde 200, no lo escribe y guarda el resto."""
    from core.alta_negocio import onboard_tenant

    slug = uniq("cfg01api")
    tid, _ = await onboard_tenant(name=f"N {slug}", slug=slug, persona={"tone": "cercano"})
    cookie = await _sesion(app_client, tid)

    actual = (await app_client.get("/api/v1/admin/settings", headers={"Cookie": cookie})).json()["identity"]
    payload = {**actual, "tono": "divertido", "debounce_mode": "corto"}
    r = await app_client.put("/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["debounce_mode"] == "corto"  # el resto se guardó
    assert "tono" not in await _behavior(tid)

    # También sin el campo (cliente nuevo): sigue funcionando.
    payload.pop("tono")
    r2 = await app_client.put("/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json=payload)
    assert r2.status_code == 200, r2.text


async def test_lectura_compatible_con_tono_guardado_de_antes(app_client) -> None:
    """Gherkin «Lectura compatible»: un `tono` viejo se sigue devolviendo y un guardado no lo cambia."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("cfg01old")
    tid, _ = await onboard_tenant(name=f"N {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid), get_session() as s:
        fila = (await s.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
        fila.behavior_config = {**dict(fila.behavior_config or {}), "tono": "formal"}
    cookie = await _sesion(app_client, tid)

    actual = (await app_client.get("/api/v1/admin/settings", headers={"Cookie": cookie})).json()["identity"]
    assert actual["tono"] == "formal"
    r = await app_client.put(
        "/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json={**actual, "tono": "divertido"}
    )
    assert r.status_code == 200, r.text
    assert (await _behavior(tid))["tono"] == "formal"  # ni se pisa ni se reescribe


async def test_cli_configure_tone_escribe_la_persona() -> None:
    """Gherkin «CLI»: `agent-core configure tone` cambia `persona.tone` y no toca `behavior_config`."""
    from cli.commands.configure import cambiar_tono_persona
    from core.agents.spec import load_active_spec
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("cfg01cli")
    tid, _ = await onboard_tenant(name=f"N {slug}", slug=slug, persona={"tone": "cercano"})
    antes = await _behavior(tid)

    assert await cambiar_tono_persona(tid, "formal") is True

    async with tenant_scope(tid):
        assert (await load_active_spec()).persona.tone == "formal"
    assert await _behavior(tid) == antes
