"""HU-CAL-04 / ADR-0091 y ADR-0092: zona horaria del negocio y acción ante el
fallo final del guardrail, editables en Configuración → Identidad.

- `timezone` se guarda en `app_settings.timezone` (default `America/Bogota`);
  una zona inexistente → 422; omitirla en el PUT no la pisa.
- `guardrail_fail_action` ∈ {escalate, neutral} (default escalate).
- `guardrail_max_retries` se acota a 0-5, igual que en el loop.
- El horario de atención usa la zona del negocio si `business_tz` viene vacío.
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


async def _cookie_for_new_tenant(app_client, prefix: str) -> str:
    cookie, _ = await _cookie_and_tenant(app_client, prefix)
    return cookie


async def _cookie_and_tenant(app_client, prefix: str):
    from core.alta_negocio import onboard_tenant

    slug = f"{prefix}-{os.urandom(3).hex()}"
    tid, _ = await onboard_tenant(
        name=f"TZ {slug}", slug=slug, business_description="Test.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303, r.text
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tid)}
    )
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session}; {tenant_cookie}", tid


async def _identity(app_client, cookie: str) -> dict:
    resp = await app_client.get("/api/v1/admin/settings", headers={"Cookie": cookie})
    assert resp.status_code == 200, resp.text
    return resp.json()["identity"]


async def _put(app_client, cookie: str, payload: dict):
    return await app_client.put("/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json=payload)


async def test_zona_horaria_por_defecto_y_editable(app_client) -> None:
    cookie = await _cookie_for_new_tenant(app_client, "tz")

    current = await _identity(app_client, cookie)
    assert current["timezone"] == "America/Bogota"
    assert current["guardrail_fail_action"] == "escalate"

    payload = dict(current, timezone="America/Mexico_City", guardrail_fail_action="neutral")
    resp = await _put(app_client, cookie, payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["timezone"] == "America/Mexico_City"
    assert resp.json()["guardrail_fail_action"] == "neutral"

    again = await _identity(app_client, cookie)
    assert again["timezone"] == "America/Mexico_City"
    assert again["guardrail_fail_action"] == "neutral"


async def test_zona_horaria_invalida_responde_422_y_no_cambia_nada(app_client) -> None:
    cookie = await _cookie_for_new_tenant(app_client, "tzbad")
    current = await _identity(app_client, cookie)

    resp = await _put(app_client, cookie, dict(current, timezone="Marte/Olimpo"))
    assert resp.status_code == 422, resp.text

    resp = await _put(app_client, cookie, dict(current, guardrail_fail_action="ignorar"))
    assert resp.status_code == 422, resp.text

    assert (await _identity(app_client, cookie))["timezone"] == "America/Bogota"


async def test_put_sin_zona_ni_accion_no_las_pisa(app_client) -> None:
    """Un cliente viejo del API (o el panel antes de actualizarse) no manda los
    campos nuevos: no deben volver a su default."""
    cookie = await _cookie_for_new_tenant(app_client, "tzkeep")
    current = await _identity(app_client, cookie)
    resp = await _put(app_client, cookie, dict(current, timezone="America/Lima", guardrail_fail_action="neutral"))
    assert resp.status_code == 200, resp.text

    legacy = {k: v for k, v in current.items() if k not in ("timezone", "guardrail_fail_action")}
    resp = await _put(app_client, cookie, legacy)
    assert resp.status_code == 200, resp.text
    assert resp.json()["timezone"] == "America/Lima"
    assert resp.json()["guardrail_fail_action"] == "neutral"


async def test_reintentos_del_guardrail_fuera_de_rango_se_rechazan(app_client) -> None:
    """Antes se recortaba a 5 en silencio; desde el P0 de pruebas 2026-10-17 el rango 0–5 se
    valida y fuera de él responde 422 con el campo marcado."""
    cookie = await _cookie_for_new_tenant(app_client, "tzret")
    current = await _identity(app_client, cookie)

    resp = await _put(app_client, cookie, dict(current, guardrail_max_retries=9))
    assert resp.status_code == 422, resp.text
    assert "guardrail_max_retries" in resp.json()["error"]["fields"]

    resp = await _put(app_client, cookie, dict(current, guardrail_max_retries=5))
    assert resp.status_code == 200, resp.text
    assert resp.json()["guardrail_max_retries"] == 5


async def test_horario_de_atencion_usa_la_zona_del_negocio(app_client) -> None:
    cookie = await _cookie_for_new_tenant(app_client, "tzhours")
    current = await _identity(app_client, cookie)

    payload = dict(
        current,
        timezone="America/Lima",
        business_tz="",
        business_ranges=[{"day": 1, "from": "09:00", "to": "18:00"}],
    )
    resp = await _put(app_client, cookie, payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["business_tz"] == "America/Lima"


async def test_horario_lunes_ida_y_vuelta_lo_aplica_el_lunes(app_client) -> None:
    """«Lunes 08:00–18:00» como lo manda el panel (day=0) se guarda así y el runtime lo
    aplica el lunes, no el martes ni el domingo (ADR-0051: 0=lunes … 6=domingo)."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.orchestrator.escalation import within_business_hours
    from core.shared.tenant import tenant_scope

    cookie, tid = await _cookie_and_tenant(app_client, "tzlunes")
    current = await _identity(app_client, cookie)
    payload = dict(
        current,
        business_tz="America/Bogota",
        business_ranges=[{"day": 0, "from": "08:00", "to": "18:00"}],
    )
    resp = await _put(app_client, cookie, payload)
    assert resp.status_code == 200, resp.text
    assert (await _identity(app_client, cookie))["business_ranges"] == [
        {"day": 0, "from": "08:00", "to": "18:00"}
    ]

    async with tenant_scope(tid):
        async with get_session() as s:
            cfg = (
                await s.execute(
                    select(AppSettings.escalation_config).where(AppSettings.tenant_id == tid)
                )
            ).scalar_one()
    business_hours = cfg["business_hours"]
    # 10:00 en Bogotá (UTC-5): lunes 2026-10-19, martes 2026-10-20 y domingo 2026-10-18.
    assert within_business_hours(business_hours, datetime(2026, 10, 19, 15, 0, tzinfo=UTC)) is True
    assert within_business_hours(business_hours, datetime(2026, 10, 20, 15, 0, tzinfo=UTC)) is False
    assert within_business_hours(business_hours, datetime(2026, 10, 18, 15, 0, tzinfo=UTC)) is False
