"""Criterio de aceptación de /admin/conexiones: el modal "Agregar conexión" se
genera desde el catálogo (ADR-0069) agrupado por categoría; los tipos `manual`
(como correo) abren un formulario genérico que guarda credenciales cifradas y la
conexión aparece listada; los no disponibles quedan como "Próximamente"."""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _switch_tenant(app_client, session_cookie: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session_cookie}, data={"tenant_id": str(tenant_id)}
    )
    ac_tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session_cookie}; {ac_tenant}"


async def _new_tenant():
    from core.alta_negocio import onboard_tenant

    slug = _uniq("selector-canal")
    tid, _ = await onboard_tenant(
        name=f"Tenant selector canal {slug}", slug=slug, business_description="Test del selector de canal.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


async def _cleanup_email(tenant_id: uuid.UUID, from_address: str) -> None:
    from sqlalchemy import select

    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ChannelConnection).where(ChannelConnection.external_id == from_address)
                )
            ).scalars().first()
            if row is not None:
                await session.delete(row)


async def test_modal_agrupado_por_categoria_con_disponibles_y_proximamente(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    r = await app_client.get("/admin/conexiones", headers={"Cookie": cookie})
    assert r.status_code == 200
    assert "Elegí el canal a conectar" in r.text
    # Categorías del catálogo.
    assert "Comunicación" in r.text
    assert "Correo" in r.text
    assert "Calendario" in r.text
    # Disponibles con su link de alta.
    assert 'href="/admin/conexiones/wizard/whatsapp/elegir-entorno"' in r.text
    assert 'href="/admin/conexiones/nuevo?type=email_smtp"' in r.text
    assert 'href="/admin/plugins/pack_calendar/oauth"' in r.text
    # No disponibles (Instagram, Twilio, Plivo).
    assert r.text.count("Próximamente") == 3
    # Messenger se conecta solo en el panel nuevo: su tarjeta no lleva a un enlace vacío.
    assert r.text.count("Solo en el panel nuevo") == 1
    assert r.text.count('aria-disabled="true"') == 4


async def test_formulario_generico_guarda_conexion_de_correo(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)
    from_address = f"{_uniq('smtp')}@ejemplo.com"
    try:
        # GET del formulario generado desde el credentials_schema del tipo.
        r = await app_client.get(
            "/admin/conexiones/nuevo?type=email_smtp", headers={"Cookie": cookie}
        )
        assert r.status_code == 200
        assert 'name="host"' in r.text
        assert 'name="from_address"' in r.text
        assert 'name="password"' in r.text  # secreto renderizado como password

        # POST: guarda cifrado y redirige al listado.
        r = await app_client.post(
            "/admin/conexiones/nuevo",
            headers={"Cookie": cookie},
            data={
                "type_id": "email_smtp",
                "label": "Correo de prueba",
                "host": "smtp.ejemplo.com",
                "port": "587",
                "username": "usuario",
                "password": "secreto",
                "from_address": from_address,
                "use_tls": "on",
            },
        )
        assert r.status_code == 303
        assert "/admin/conexiones?" in r.headers["location"]

        r = await app_client.get("/admin/conexiones", headers={"Cookie": cookie})
        assert from_address in r.text
        assert "Correo (SMTP)" in r.text or "Correo" in r.text
    finally:
        await _cleanup_email(tid, from_address)


async def test_formulario_invalido_redirige(app_client):
    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    # Un tipo wizard no se sirve por el formulario genérico.
    r = await app_client.get(
        "/admin/conexiones/nuevo?type=whatsapp_meta", headers={"Cookie": cookie}
    )
    assert r.status_code == 303
    from urllib.parse import unquote

    assert "Tipo de conexión inválido" in unquote(r.headers["location"])


async def _cleanup_all(tenant_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(
                delete(ChannelConnection).where(ChannelConnection.tenant_id == tenant_id)
            )


async def test_editar_conexion_manual_conserva_secretos(app_client):
    from core.gateway.connections import resolve_connection_credentials
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)
    original = f"{_uniq('edit')}@ejemplo.com"
    nuevo = f"{_uniq('edit2')}@ejemplo.com"

    await app_client.post(
        "/admin/conexiones/nuevo",
        headers={"Cookie": cookie},
        data={
            "type_id": "email_smtp", "label": "Correo", "host": "smtp.viejo.com",
            "port": "587", "username": "u", "password": "secreto1",
            "from_address": original, "use_tls": "on",
        },
    )
    async with tenant_scope(tid):
        creds = await resolve_connection_credentials("email_smtp", tid)
    assert creds is not None and creds["host"] == "smtp.viejo.com"
    connection_id = (await _get_email_connection_id(tid))

    # GET de edición: prellena el remitente.
    r = await app_client.get(
        f"/admin/conexiones/nuevo?type=email_smtp&connection_id={connection_id}",
        headers={"Cookie": cookie},
    )
    assert r.status_code == 200
    assert original in r.text

    # POST de edición: cambia host/remitente y deja el secreto en blanco.
    r = await app_client.post(
        "/admin/conexiones/nuevo",
        headers={"Cookie": cookie},
        data={
            "type_id": "email_smtp", "connection_id": str(connection_id),
            "label": "Correo editado", "host": "smtp.nuevo.com", "port": "587",
            "username": "u", "password": "", "from_address": nuevo, "use_tls": "on",
        },
    )
    assert r.status_code == 303

    async with tenant_scope(tid):
        creds = await resolve_connection_credentials("email_smtp", tid)
    assert creds is not None
    assert creds["host"] == "smtp.nuevo.com"
    assert creds["from_address"] == nuevo
    assert creds["password"] == "secreto1", "el secreto en blanco se conserva"

    await _cleanup_all(tid)


async def _get_email_connection_id(tenant_id: uuid.UUID) -> uuid.UUID:
    from core.gateway.connections import get_active_connection
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        conn = await get_active_connection("email_smtp", tenant_id)
    assert conn is not None
    return conn.id