"""Alta de un negocio desde el panel: el dueño recibe su acceso por correo (ADR-0175).

La cuenta del dueño nace sin contraseña y le llega un enlace de un solo uso; si el correo no
sale el negocio se crea igual y la respuesta lo dice; el reenvío es explícito."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import api_headers
from sqlalchemy import select, update

pytestmark = [
    pytest.mark.aislamiento,  # gestión de cuentas y de accesos: subconjunto de las PRs (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/platform/tenants"


def _correo(prefix: str = "dueno") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.test"


def _slug() -> str:
    return f"alta-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def correos(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Correo de sistema capturado (sin SMTP real)."""
    enviados: list[dict[str, str]] = []

    async def falso(to: str, subject: str, body: str) -> str:
        enviados.append({"to": to, "subject": subject, "body": body})
        return "msg-test"

    monkeypatch.setattr("core.gateway.system_email.send_system_email", falso)
    return enviados


def _token_del(correo: dict[str, str]) -> str:
    encontrado = re.search(r"/invitacion#(\S+)", correo["body"])
    assert encontrado, correo["body"]
    return encontrado.group(1)


async def _alta(app_client, headers, *, email: str | None = None, slug: str | None = None, **extra):
    cuerpo = {"name": "Negocio Panel", "slug": slug or _slug(), **extra}
    if email is not None:
        cuerpo["admin_email"] = email
    return await app_client.post(URL, json=cuerpo, headers=headers)


async def _cuenta(email: str):
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()


async def _invitaciones(tenant_id: str):
    from core.db.models import TenantInvitation
    from core.db.session import get_session

    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(TenantInvitation).where(TenantInvitation.tenant_id == uuid.UUID(tenant_id))
                )
            ).scalars()
        )


async def _miembros(tenant_id: str):
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        return list(
            (await session.execute(select(TenantUser).where(TenantUser.tenant_id == uuid.UUID(tenant_id)))).scalars()
        )


async def _vencer(tenant_id: str) -> None:
    from core.db.models import TenantInvitation
    from core.db.session import get_session

    async with get_session() as session:
        await session.execute(
            update(TenantInvitation)
            .where(TenantInvitation.tenant_id == uuid.UUID(tenant_id))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )


async def test_alta_nueva_manda_el_enlace_y_el_canje_deja_entrar(app_client, tenant_setup, correos) -> None:
    from core.admin.invitations_service import SIN_CONTRASENA

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _correo()
    r = await _alta(app_client, master, email=email)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["invitation"]["estado"] == "enviado"
    assert cuerpo["invitation"]["tipo"] == "enlace"
    assert cuerpo["invitation"]["expires_at"]
    # Los pasos no dicen «sin admin»: el dueño existe y entra por la invitación.
    assert not any(p.startswith("sin admin") for p in cuerpo["steps"])
    assert "acceso del dueño: invitación enlace, correo enviado" in cuerpo["steps"]

    # Un solo correo, al correo del dueño, con el enlace de un solo uso.
    assert len(correos) == 1
    assert correos[0]["to"] == email
    assert correos[0]["subject"] == "Tu acceso a SolversHub"
    token = _token_del(correos[0])
    # El enlace NUNCA vuelve en la respuesta.
    assert token not in r.text

    # La cuenta nació sin contraseña, sin membresía y con una invitación pendiente con nonce.
    cuenta = await _cuenta(email)
    assert cuenta is not None and cuenta.password_hash == SIN_CONTRASENA
    assert await _miembros(cuerpo["tenant_id"]) == []
    invitaciones = await _invitaciones(cuerpo["tenant_id"])
    assert [(i.estado, i.role, bool(i.token_nonce)) for i in invitaciones] == [("pendiente", "owner", True)]

    # El canje define la contraseña, crea la membresía y abre la sesión.
    r = await app_client.post("/api/v1/auth/accept-invitation", json={"token": token, "new_password": "ClaveNueva-123"})
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == cuerpo["tenant_id"]
    assert [m.role for m in await _miembros(cuerpo["tenant_id"])] == ["owner"]
    # Uso único.
    r = await app_client.post("/api/v1/auth/accept-invitation", json={"token": token, "new_password": "OtraClave-123"})
    assert r.status_code == 400, r.text


async def test_enlace_vencido_no_canjea(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    r = await _alta(app_client, master, email=_correo())
    assert r.status_code == 201, r.text
    await _vencer(r.json()["tenant_id"])
    r = await app_client.post(
        "/api/v1/auth/accept-invitation", json={"token": _token_del(correos[0]), "new_password": "ClaveNueva-123"}
    )
    assert r.status_code == 400, r.text


async def test_doble_post_no_repite_el_correo(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email, slug = _correo(), _slug()
    primero = await _alta(app_client, master, email=email, slug=slug)
    segundo = await _alta(app_client, master, email=email, slug=slug)
    assert primero.status_code == 201 and segundo.status_code == 201, segundo.text
    assert primero.json()["tenant_id"] == segundo.json()["tenant_id"]
    assert segundo.json()["invitation"]["estado"] == "ya_emitida"
    assert len(correos) == 1
    assert len(await _invitaciones(primero.json()["tenant_id"])) == 1


async def test_smtp_caido_crea_el_negocio_y_el_reenvio_manda_el_mismo_enlace(
    app_client, tenant_setup, monkeypatch
) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)

    async def caido(to: str, subject: str, body: str) -> str:
        raise OSError("smtp caído")

    monkeypatch.setattr("core.gateway.system_email.send_system_email", caido)
    email = _correo()
    r = await _alta(app_client, master, email=email)
    assert r.status_code == 201, r.text
    assert r.json()["invitation"]["estado"] == "fallido"
    tenant_id = r.json()["tenant_id"]
    assert [i.estado for i in await _invitaciones(tenant_id)] == ["pendiente"]

    enviados: list[dict[str, str]] = []

    async def bueno(to: str, subject: str, body: str) -> str:
        enviados.append({"to": to, "subject": subject, "body": body})
        return "msg"

    monkeypatch.setattr("core.gateway.system_email.send_system_email", bueno)
    r = await app_client.post(f"{URL}/{tenant_id}/owner-invitation/resend", headers=master)
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "enviado" and r.json()["tipo"] == "enlace"
    assert enviados[0]["to"] == email
    # El enlace sigue vigente: se reusa el nonce, así que el correo trae un token que canjea.
    token = _token_del(enviados[0])
    assert token not in r.text
    r = await app_client.post("/api/v1/auth/accept-invitation", json={"token": token, "new_password": "ClaveNueva-123"})
    assert r.status_code == 200, r.text


async def test_sin_smtp_configurado_se_informa(app_client, tenant_setup, monkeypatch) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)

    async def sin_smtp(to: str, subject: str, body: str) -> None:
        return None

    monkeypatch.setattr("core.gateway.system_email.send_system_email", sin_smtp)
    r = await _alta(app_client, master, email=_correo())
    assert r.status_code == 201, r.text
    assert r.json()["invitation"]["estado"] == "sin_smtp"


async def test_reenvio_con_enlace_vencido_lo_renueva(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    r = await _alta(app_client, master, email=_correo())
    tenant_id = r.json()["tenant_id"]
    viejo = _token_del(correos[0])
    await _vencer(tenant_id)

    r = await app_client.post(f"{URL}/{tenant_id}/owner-invitation/resend", headers=master)
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "enviado"
    nuevo = _token_del(correos[1])
    assert nuevo != viejo
    # El enlace viejo ya no vale; el nuevo sí.
    r = await app_client.post("/api/v1/auth/accept-invitation", json={"token": viejo, "new_password": "ClaveNueva-123"})
    assert r.status_code == 400, r.text
    r = await app_client.post("/api/v1/auth/accept-invitation", json={"token": nuevo, "new_password": "ClaveNueva-123"})
    assert r.status_code == 200, r.text


async def test_reenvio_rechazado_sin_pendiente_o_con_negocio_inexistente(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    r = await _alta(app_client, master, email=_correo())
    tenant_id = r.json()["tenant_id"]
    canje = await app_client.post(
        "/api/v1/auth/accept-invitation", json={"token": _token_del(correos[0]), "new_password": "ClaveNueva-123"}
    )
    assert canje.status_code == 200, canje.text
    # Ya aceptó: no hay nada que reenviar.
    r = await app_client.post(f"{URL}/{tenant_id}/owner-invitation/resend", headers=master)
    assert r.status_code == 409 and r.json()["error"]["code"] == "sin_invitacion_pendiente", r.text
    r = await app_client.post(f"{URL}/{uuid.uuid4()}/owner-invitation/resend", headers=master)
    assert r.status_code == 404, r.text
    # Sin sesión no hay reenvío.
    r = await app_client.post(f"{URL}/{tenant_id}/owner-invitation/resend")
    assert r.status_code in (401, 403), r.text


async def test_cuenta_existente_recibe_la_invitacion_sin_enlace(app_client, tenant_setup, correos) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _correo("existente")
    async with get_session() as session:
        session.add(Admin(email=email, password_hash=hash_password("Contrasena-123"), role="colaborador", active=True))

    r = await _alta(app_client, master, email=email)
    assert r.status_code == 201, r.text
    assert r.json()["invitation"] == {"estado": "enviado", "tipo": "app", "expires_at": None}
    assert "#" not in correos[0]["body"]
    assert correos[0]["subject"] == "Te dieron acceso a un negocio en SolversHub"
    # Sin membresía hasta que acepte en la app, y su contraseña no se tocó.
    assert await _miembros(r.json()["tenant_id"]) == []
    cuenta = await _cuenta(email)
    assert cuenta is not None and cuenta.password_hash != "!sin-contrasena:invitacion"
    pendiente = (await _invitaciones(r.json()["tenant_id"]))[0]
    assert pendiente.token_nonce is None


async def test_correos_que_no_se_pueden_invitar_no_crean_el_negocio(app_client, tenant_setup, correos) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    slug = _slug()
    r = await _alta(app_client, master, email="admin@local", slug=slug)  # el master
    assert r.status_code == 409 and r.json()["error"]["code"] == "owner_email_rejected", r.text
    async with get_session() as session:
        assert (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).first() is None
    assert correos == []


async def test_contrasena_en_el_cuerpo_ya_no_se_acepta(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    r = await _alta(app_client, master, email=_correo(), admin_password="Passw0rd!")
    assert r.status_code == 400 and r.json()["error"]["code"] == "admin_password_no_soportado", r.text
    r = await _alta(app_client, master, email=_correo(), admin_password="")
    assert r.status_code == 201, r.text
    # Correo con forma inválida.
    r = await _alta(app_client, master, email="no-es-un-correo")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_owner_email", r.text


async def test_sin_correo_del_dueno_no_hay_invitacion(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    r = await _alta(app_client, master)
    assert r.status_code == 201, r.text
    assert r.json()["invitation"] is None
    assert correos == []


async def test_dos_altas_a_la_vez_con_el_mismo_slug_no_dan_500(app_client, tenant_setup, correos) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    slug = _slug()
    respuestas = await asyncio.gather(
        _alta(app_client, master, email=_correo(), slug=slug),
        _alta(app_client, master, email=_correo(), slug=slug),
    )
    assert all(r.status_code in (201, 409) for r in respuestas), [r.text for r in respuestas]
    assert any(r.status_code == 201 for r in respuestas)


async def test_el_acceso_no_filtra_el_token_ni_el_correo_en_logs_ni_auditoria(
    app_client, tenant_setup, correos, caplog
) -> None:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _correo("secreto")
    with caplog.at_level("DEBUG"):
        r = await _alta(app_client, master, email=email)
    assert r.status_code == 201, r.text
    token = _token_del(correos[0])
    assert token not in caplog.text
    assert email not in caplog.text

    tenant_id = uuid.UUID(r.json()["tenant_id"])
    async with tenant_scope(tenant_id), get_session() as session:
        filas = list(
            (await session.execute(select(AuditLog).where(AuditLog.action == "tenant.owner_access"))).scalars()
        )
    assert len(filas) == 1
    salida = str(filas[0].output)
    assert token not in salida and email not in salida
    assert filas[0].output["estado"] == "enviado" and filas[0].output["reenvio"] is False
