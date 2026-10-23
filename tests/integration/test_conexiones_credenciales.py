"""Credenciales por conexión y eliminación con contraseña (step-up).

- Los datos no secretos se leen sin contraseña; los secretos solo se revelan con la
  contraseña del usuario logueado (400 sin contraseña, 403 incorrecta).
- La vista de Meta nunca expone el App Secret ni el verify token de la **app de la
  plataforma** (Embedded Signup / carga manual); sí los de una conexión BYOK.
- Eliminar una conexión (canal o Meta) exige confirmación y contraseña; el borrado es
  soft-delete y queda auditado.
- Una conexión de otro negocio no se ve (404).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import admin_password, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _tenant() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("creds")
    tid, _ = await onboard_tenant(
        name=f"Tenant creds {slug}",
        slug=slug,
        business_description="Test de credenciales y eliminación de conexiones.",
        persona={"tone": "cercano"},
        admin_email="admin@local",
    )
    return tid


async def _manual(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.gateway.connection_types import CATEGORY_EMAIL
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = ChannelConnection(
                tenant_id=tid,
                channel="email",
                provider="smtp",
                type_id="email_smtp",
                category=CATEGORY_EMAIL,
                label="Correo de prueba",
                active=True,
                external_id=uniq("mail"),
                credentials_encrypted=get_secret_box().encrypt_json(
                    {
                        "host": "smtp.example",
                        "port": 587,
                        "username": "usuario",
                        "password": "secreto-smtp",
                        "from_address": "a@example.com",
                        "use_tls": True,
                    }
                ),
            )
            session.add(fila)
            await session.flush()
            return fila.id


async def _meta(tid: uuid.UUID, *, origin: str = "byok", app_id: str | None = None) -> uuid.UUID:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = MetaConnection(
                tenant_id=tid,
                channel="whatsapp",
                environment="sandbox",
                origin=origin,
                meta_app_id=app_id or uniq("app"),
                status="connected",
            )
            session.add(fila)
            await session.flush()
            return fila.id


async def _tokens(tid: uuid.UUID, cid: uuid.UUID, app_id: str) -> None:
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import (
        APP_SECRET_KIND,
        CLOUD_API_ACCESS_TOKEN_KIND,
        WEBHOOK_VERIFY_TOKEN_KIND,
    )
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await meta_token_store.put_token(cid, APP_SECRET_KIND, app_id, "secreto-app", tenant_id=tid)
        await meta_token_store.put_token(
            cid, CLOUD_API_ACCESS_TOKEN_KIND, app_id, "token-acceso", tenant_id=tid
        )
        await meta_token_store.put_token(
            cid, WEBHOOK_VERIFY_TOKEN_KIND, app_id, "verifica-app", tenant_id=tid
        )


async def test_credenciales_manuales_y_step_up(app_client):
    tid = await _tenant()
    cid = await _manual(tid)
    h = await api_headers(app_client, tid)

    r = await app_client.get(f"/api/v1/admin/connections/{cid}/credentials", headers=h)
    assert r.status_code == 200, r.text
    campos = {f["name"]: f for f in r.json()["fields"]}
    assert campos["host"]["value"] == "smtp.example"
    assert campos["password"]["secret"] is True
    assert campos["password"]["configured"] is True
    assert campos["password"]["value"] is None

    reveal = f"/api/v1/admin/connections/{cid}/credentials/reveal"
    assert (await app_client.post(reveal, headers=h, json={})).status_code == 400
    assert (
        await app_client.post(reveal, headers=h, json={"password": "incorrecta"})
    ).status_code == 403
    ok = await app_client.post(reveal, headers=h, json={"password": admin_password()})
    assert ok.status_code == 200, ok.text
    assert ok.json()["secrets"]["password"] == "secreto-smtp"
    assert "host" not in ok.json()["secrets"]


async def test_eliminar_conexion_exige_confirmacion_y_password(app_client):
    from test_kernel_ingress import _filas

    tid = await _tenant()
    cid = await _manual(tid)
    h = await api_headers(app_client, tid)
    accion = f"/api/v1/admin/connections/{cid}/action"

    assert (await app_client.post(accion, headers=h, json={"action": "delete"})).status_code == 400
    assert (
        await app_client.post(
            accion, headers=h, json={"action": "delete", "password": "incorrecta"}
        )
    ).status_code == 403
    ok = await app_client.post(
        accion, headers=h, json={"action": "delete", "password": admin_password()}
    )
    assert ok.status_code == 204, ok.text

    (fila,) = await _filas(tid, "SELECT deleted_at FROM channel_connections WHERE id = :c", c=cid)
    assert fila["deleted_at"] is not None
    auditoria = await _filas(
        tid,
        "SELECT action FROM audit_log WHERE tenant_id = :t AND input->>'connection_id' = :c "
        "AND action = 'connection.deleted'",
        t=tid,
        c=str(cid),
    )
    assert auditoria


async def test_credenciales_meta_byok_y_eliminar(app_client):
    tid = await _tenant()
    app_id = uniq("app")
    cid = await _meta(tid, app_id=app_id)
    await _tokens(tid, cid, app_id)
    h = await api_headers(app_client, tid)

    r = await app_client.get(f"/api/v1/admin/meta/connections/{cid}/credentials", headers=h)
    assert r.status_code == 200, r.text
    campos = {f["name"]: f for f in r.json()["fields"]}
    assert campos["app_id"]["value"] == app_id
    assert campos["app_secret"]["configured"] is True
    assert campos["app_secret"]["platform"] is False
    assert campos["verify_token"]["configured"] is True

    reveal = f"/api/v1/admin/meta/connections/{cid}/credentials/reveal"
    ok = await app_client.post(reveal, headers=h, json={"password": admin_password()})
    assert ok.status_code == 200, ok.text
    secretos = ok.json()["secrets"]
    assert secretos["app_secret"] == "secreto-app"
    assert secretos["access_token"] == "token-acceso"
    assert secretos["verify_token"] == "verifica-app"

    accion = f"/api/v1/admin/meta/connections/{cid}/action"
    assert (await app_client.post(accion, headers=h, json={"action": "delete"})).status_code == 400
    assert (
        await app_client.post(
            accion, headers=h, json={"action": "delete", "password": "incorrecta"}
        )
    ).status_code == 403
    assert (
        await app_client.post(
            accion, headers=h, json={"action": "delete", "password": admin_password()}
        )
    ).status_code == 204


async def test_meta_no_revela_los_secretos_de_la_plataforma(app_client, monkeypatch):
    from core.gateway.meta_credentials import MetaCredentials

    tid = await _tenant()
    cid = await _meta(tid, origin="embedded_signup")
    h = await api_headers(app_client, tid)

    async def credenciales_falsas(connection, *, waba_id=None):
        return MetaCredentials(
            connection_id=connection.id,
            tenant_id=connection.tenant_id,
            origin=connection.origin,
            app_id="app-plataforma",
            graph_api_version=connection.graph_api_version,
            access_token="token-conexion",
            app_secret="secreto-plataforma",
        )

    monkeypatch.setattr("core.gateway.meta_credentials.credentials_for", credenciales_falsas)

    r = await app_client.get(f"/api/v1/admin/meta/connections/{cid}/credentials", headers=h)
    assert r.status_code == 200, r.text
    campos = {f["name"]: f for f in r.json()["fields"]}
    assert campos["app_secret"]["platform"] is True
    assert campos["verify_token"]["platform"] is True

    ok = await app_client.post(
        f"/api/v1/admin/meta/connections/{cid}/credentials/reveal",
        headers=h,
        json={"password": admin_password()},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["secrets"] == {"access_token": "token-conexion"}


async def test_credenciales_de_otro_negocio_no_se_ven(app_client):
    tid1 = await _tenant()
    cid = await _manual(tid1)
    tid2 = await _tenant()
    h2 = await api_headers(app_client, tid2)

    assert (
        await app_client.get(f"/api/v1/admin/connections/{cid}/credentials", headers=h2)
    ).status_code == 404
    assert (
        await app_client.post(
            f"/api/v1/admin/connections/{cid}/credentials/reveal",
            headers=h2,
            json={"password": admin_password()},
        )
    ).status_code == 404
