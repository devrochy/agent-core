"""Pausa del asistente del negocio desde la barra superior del panel (HU-UI-03).

`PUT /api/v1/admin/settings/bot-paused` cambia solo `app_settings.bot_paused`:
- no pisa el resto de la identidad (antes el panel reenviaba la identidad
  completa y ganaba la última escritura);
- solo afecta al negocio activo de la sesión, nunca a otro;
- exige escritura en `settings` (un colaborador de solo lectura recibe 403);
- deja rastro en `audit_log` cuando el valor cambia, y repetirlo no audita de nuevo;
- `PUT .../identity` sin `bot_paused` no reactiva un asistente pausado.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/settings/bot-paused"
PASSWORD = "clave-segura-123"


async def _negocio(prefijo: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = f"{prefijo}-{os.urandom(3).hex()}"
    tid, _ = await onboard_tenant(
        name=f"Pausa {slug}", slug=slug, business_description="Descripción original.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


async def _cookie(app_client, tenant_id: uuid.UUID) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303, r.text
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tenant_id)}
    )
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session}; {tenant_cookie}"


async def _identity(app_client, cookie: str) -> dict:
    resp = await app_client.get("/api/v1/admin/settings", headers={"Cookie": cookie})
    assert resp.status_code == 200, resp.text
    return resp.json()["identity"]


async def _auditoria(tenant_id: uuid.UUID) -> list[tuple[str, str]]:
    from sqlalchemy import select

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(AuditLog.actor, AuditLog.action)
                    .where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("settings.bot_paused", "settings.bot_resumed")),
                    )
                    .order_by(AuditLog.created_at)
                )
            ).all()
    return [(r.actor, r.action) for r in rows]


async def test_pausa_solo_cambia_bot_paused_y_audita(app_client) -> None:
    tid = await _negocio("pausa")
    cookie = await _cookie(app_client, tid)

    antes = await _identity(app_client, cookie)
    payload = dict(antes, business_description="Descripción editada.", persona_warmth=5)
    resp = await app_client.put("/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json=payload)
    assert resp.status_code == 200, resp.text
    editada = await _identity(app_client, cookie)
    assert editada["persona_warmth"] == 5
    assert editada["bot_paused"] is False

    resp = await app_client.put(URL, headers={"Cookie": cookie}, json={"paused": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"paused": True}

    despues = await _identity(app_client, cookie)
    assert despues["bot_paused"] is True
    assert {k: v for k, v in despues.items() if k != "bot_paused"} == {
        k: v for k, v in editada.items() if k != "bot_paused"
    }

    # Repetir no cambia nada ni vuelve a auditar; reactivar sí.
    resp = await app_client.put(URL, headers={"Cookie": cookie}, json={"paused": True})
    assert resp.status_code == 200, resp.text
    resp = await app_client.put(URL, headers={"Cookie": cookie}, json={"paused": False})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"paused": False}
    assert (await _identity(app_client, cookie))["bot_paused"] is False

    assert await _auditoria(tid) == [
        ("admin:admin@local", "settings.bot_paused"),
        ("admin:admin@local", "settings.bot_resumed"),
    ]


async def test_guardar_identidad_sin_bot_paused_no_lo_cambia(app_client) -> None:
    """Quien tenía Configuración abierta desde antes de la pausa no la deshace al guardar."""
    tid = await _negocio("pausa-ident")
    cookie = await _cookie(app_client, tid)
    abierta = await _identity(app_client, cookie)

    resp = await app_client.put(URL, headers={"Cookie": cookie}, json={"paused": True})
    assert resp.status_code == 200, resp.text

    sin_pausa = {k: v for k, v in abierta.items() if k != "bot_paused"}
    resp = await app_client.put(
        "/api/v1/admin/settings/identity",
        headers={"Cookie": cookie},
        json=dict(sin_pausa, business_description="Cambio de otra persona."),
    )
    assert resp.status_code == 200, resp.text
    despues = await _identity(app_client, cookie)
    assert despues["bot_paused"] is True
    assert despues["business_description"] == "Cambio de otra persona."

    # Si lo manda explícito, sí lo cambia (el interruptor del formulario).
    resp = await app_client.put(
        "/api/v1/admin/settings/identity", headers={"Cookie": cookie}, json=dict(sin_pausa, bot_paused=False)
    )
    assert resp.status_code == 200, resp.text
    assert (await _identity(app_client, cookie))["bot_paused"] is False


async def test_pausa_no_toca_otro_negocio(app_client) -> None:
    tid_a = await _negocio("pausa-a")
    tid_b = await _negocio("pausa-b")
    cookie_a = await _cookie(app_client, tid_a)

    resp = await app_client.put(URL, headers={"Cookie": cookie_a}, json={"paused": True})
    assert resp.status_code == 200, resp.text

    cookie_b = await _cookie(app_client, tid_b)
    assert (await _identity(app_client, cookie_b))["bot_paused"] is False
    assert await _auditoria(tid_b) == []
    assert (await _identity(app_client, cookie_a))["bot_paused"] is True


async def test_negocio_ajeno_por_header_o_cookie_no_se_toca(app_client) -> None:
    """Un owner de A no puede pausar B con `X-Tenant-Id` (403) ni con la cookie de negocio."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid_a = await _negocio("pausa-h-a")
    tid_b = await _negocio("pausa-h-b")
    email = f"owner-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid_a, admin_id=admin.id, role="owner"))

    r = await app_client.post("/admin/login", data={"email": email, "password": PASSWORD})
    assert r.status_code == 303, r.text
    cookie = r.headers["set-cookie"].split(";")[0]

    resp = await app_client.put(
        URL, headers={"Cookie": cookie, "X-Tenant-Id": str(tid_b)}, json={"paused": True}
    )
    assert resp.status_code == 403, resp.text

    # Una cookie de negocio ajena se ignora: cae en su propio negocio (A), no en B.
    resp = await app_client.put(
        URL, headers={"Cookie": f"{cookie}; ac_tenant={tid_b}"}, json={"paused": True}
    )
    assert resp.status_code == 200, resp.text

    admin_b = await _cookie(app_client, tid_b)
    assert (await _identity(app_client, admin_b))["bot_paused"] is False
    assert await _auditoria(tid_b) == []
    assert await _auditoria(tid_a) == [(f"admin:{email}", "settings.bot_paused")]


async def test_colaborador_de_solo_lectura_recibe_403(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _negocio("pausa-colab")
    email = f"colab-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))

    r = await app_client.post("/admin/login", data={"email": email, "password": PASSWORD})
    assert r.status_code == 303, r.text
    cookie = r.headers["set-cookie"].split(";")[0]

    resp = await app_client.put(URL, headers={"Cookie": cookie}, json={"paused": True})
    assert resp.status_code == 403, resp.text
    assert await _auditoria(tid) == []
