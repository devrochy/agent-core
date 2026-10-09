"""Enlace de un solo uso de la invitación al owner (HU-SH-03, ADR-0121 § 3.8).

`POST /api/v1/auth/accept-invitation`: el owner de un negocio creado por el alta
automática define su contraseña y queda como miembro. Uso único, vencimiento, firma,
cuenta que no es nueva y limpia (con contraseña, otra membresía u otra invitación
pendiente), carrera con un reset de contraseña y bloqueo progresivo. También el
vencimiento al aceptar una invitación en la app (ADR-0081).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

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

ACEPTAR = "/api/v1/auth/accept-invitation"
LOGIN = "/api/v1/auth/login"
CLAVE = "clave-nueva-123"


@pytest.fixture(autouse=True)
async def _sin_bloqueos_de_invitacion():
    """El bloqueo progresivo vive en Redis (`auth:*:invitacion:*`): cada test arranca limpio."""
    from core.memory.working import get_redis

    redis = get_redis()
    for clave in await redis.keys("auth:*:invitacion:*"):
        await redis.delete(clave)
    yield


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("invitado")
    tenant_id, _ = await onboard_tenant(name=f"Negocio {slug}", slug=slug)
    return tenant_id


async def _invitacion(*, con_contrasena: bool = False) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    """Negocio + cuenta del owner + invitación `owner`. Devuelve (tenant, invitación, correo, token)."""
    from core.admin.auth import hash_password
    from core.admin.invitations_service import (
        SIN_CONTRASENA,
        crear_invitacion_owner,
        token_de_invitacion,
    )
    from core.db.models import Admin
    from core.db.session import get_session

    tenant_id = await _negocio()
    email = f"{uniq('owner')}@correo-prueba.co"
    async with get_session() as s:
        admin = Admin(
            email=email,
            password_hash=hash_password("clave-propia-1") if con_contrasena else SIN_CONTRASENA,
            role="colaborador",
        )
        s.add(admin)
        await s.flush()
        admin_id = admin.id
    invitacion, error = await crear_invitacion_owner(
        tenant_id=tenant_id, admin_id=admin_id, con_enlace=True, vigencia=timedelta(days=7)
    )
    assert error is None and invitacion is not None and invitacion.token_nonce
    return tenant_id, invitacion.id, email, token_de_invitacion(invitacion.id, invitacion.token_nonce)


async def _leer_invitacion(invitation_id: uuid.UUID):  # type: ignore[no-untyped-def]
    from core.db.models import TenantInvitation
    from core.db.session import get_session

    async with get_session() as s:
        return await s.get(TenantInvitation, invitation_id)


async def _admin_de(email: str):  # type: ignore[no-untyped-def]
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as s:
        return (await s.execute(select(Admin).where(Admin.email == email))).scalar_one()


async def test_el_enlace_define_la_contrasena_y_deja_al_owner_en_su_negocio(app_client) -> None:
    from core.db.models import AuditLog, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id, invitation_id, email, token = await _invitacion()

    r = await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})
    assert r.status_code == 204, r.text

    login = await app_client.post(LOGIN, json={"identifier": email, "password": CLAVE})
    assert login.status_code == 200, login.text
    invitacion = await _leer_invitacion(invitation_id)
    assert (invitacion.estado, invitacion.token_nonce) == ("aceptada", None)
    async with get_session() as s:
        vinculo = (
            await s.execute(select(TenantUser).where(TenantUser.tenant_id == tenant_id))
        ).scalar_one()
    assert vinculo.role == "owner"
    async with tenant_scope(tenant_id):
        async with get_session() as s:
            auditado = (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id, AuditLog.action == "invitacion_aceptada"
                    )
                )
            ).scalar_one()
    assert auditado.actor == f"admin:{email}"


async def test_el_enlace_es_de_un_solo_uso(app_client) -> None:
    _, _, email, token = await _invitacion()
    assert (await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})).status_code == 204

    otra = await app_client.post(ACEPTAR, json={"token": token, "new_password": "otra-clave-999"})
    assert otra.status_code == 400
    assert otra.json()["error"]["code"] == "invalid_token"
    # La contraseña sigue siendo la del primer canje.
    login = await app_client.post(LOGIN, json={"identifier": email, "password": "otra-clave-999"})
    assert login.status_code == 401


async def test_un_enlace_alterado_o_vencido_no_sirve(app_client) -> None:
    from core.db.models import TenantInvitation
    from core.db.session import get_session

    _, invitation_id, _, token = await _invitacion()
    alterado = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    for malo in (alterado, f"{uuid.uuid4()}.{token.split('.', 1)[1]}", "basura"):
        r = await app_client.post(ACEPTAR, json={"token": malo, "new_password": CLAVE})
        assert r.status_code == 400, malo

    async with get_session() as s:
        fila = await s.get(TenantInvitation, invitation_id)
        fila.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    vencido = await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})
    assert vencido.status_code == 400
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"


async def test_una_cuenta_con_contrasena_no_se_cambia_por_enlace(app_client) -> None:
    """Un enlace no prueba que el dueño del correo consienta: entra y acepta en la app."""
    _, invitation_id, email, token = await _invitacion(con_contrasena=True)

    r = await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "account_exists"
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"
    login = await app_client.post(LOGIN, json={"identifier": email, "password": "clave-propia-1"})
    assert login.status_code == 200


@pytest.mark.parametrize("otro_acceso", ["membresia", "invitacion_pendiente"])
async def test_una_cuenta_con_otro_acceso_no_se_toma_con_el_enlace(app_client, otro_acceso: str) -> None:
    """Si la cuenta ya entra a otro negocio, o tiene una invitación pendiente en otro, quien
    tiene el enlace no puede quedarse con ella (ni con esa otra invitación)."""
    from core.admin.invitations_service import crear_invitacion
    from core.db.models import TenantUser
    from core.db.session import get_session

    _, invitation_id, email, token = await _invitacion()
    admin = await _admin_de(email)
    otro = await _negocio()
    if otro_acceso == "membresia":
        async with get_session() as s:
            s.add(TenantUser(tenant_id=otro, admin_id=admin.id, role="colaborador"))
    else:
        assert await crear_invitacion(tenant_id=otro, admin_id=admin.id, invited_by=None) is None

    r = await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "account_exists"
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"
    assert (await _admin_de(email)).password_hash == admin.password_hash


async def test_un_reset_que_llega_antes_gana_y_el_canje_no_pisa_la_contrasena(app_client, monkeypatch) -> None:
    """Carrera canje/reset: la contraseña se escribe con un UPDATE condicional sobre la cuenta
    sin contraseña; si un reset la definió en el medio, el canje no la pisa."""
    from core.admin import invitations_service
    from core.admin.auth import hash_password, verify_password
    from core.db.models import Admin
    from core.db.session import get_session

    _, invitation_id, email, token = await _invitacion()
    original = invitations_service._tiene_otro_acceso

    async def con_reset_en_el_medio(session, admin_id, tenant_id):  # type: ignore[no-untyped-def]
        async with get_session() as otra:  # otra transacción, como un reset concurrente
            cuenta = await otra.get(Admin, admin_id)
            cuenta.password_hash = hash_password("del-reset-123")
        return await original(session, admin_id, tenant_id)

    monkeypatch.setattr(invitations_service, "_tiene_otro_acceso", con_reset_en_el_medio)
    r = await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})
    assert r.status_code == 409
    assert verify_password("del-reset-123", (await _admin_de(email)).password_hash)
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"


async def test_aceptar_en_la_app_una_invitacion_vencida_no_da_acceso() -> None:
    from core.admin.invitations_service import INVITACION_VENCIDA, aceptar_invitacion, pendientes_de
    from core.db.models import TenantInvitation, TenantUser
    from core.db.session import get_session

    tenant_id, invitation_id, email, _ = await _invitacion()
    async with get_session() as s:
        fila = await s.get(TenantInvitation, invitation_id)
        fila.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    admin = await _admin_de(email)

    assert await pendientes_de(admin.id) == []  # vencida: ya no se ofrece en la app
    assert await aceptar_invitacion(invitation_id=invitation_id, admin_id=admin.id) == INVITACION_VENCIDA
    async with get_session() as s:
        vinculos = (
            await s.execute(select(TenantUser).where(TenantUser.tenant_id == tenant_id))
        ).scalars().all()
    assert vinculos == []
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"


async def test_contrasena_corta_se_rechaza_sin_gastar_el_enlace(app_client) -> None:
    _, invitation_id, _, token = await _invitacion()
    r = await app_client.post(ACEPTAR, json={"token": token, "new_password": "corta"})
    assert r.status_code == 422
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"


async def test_bloqueo_progresivo_ante_muchos_intentos(app_client) -> None:
    """Mismo bloqueo que el login: 5 fallos en 10 minutos → 429 durante 15 minutos."""
    _, invitation_id, _, token = await _invitacion()
    falso = f"{invitation_id}.firma-que-no-es"
    for _ in range(5):
        r = await app_client.post(ACEPTAR, json={"token": falso, "new_password": CLAVE})
        assert r.status_code == 400
    bloqueado = await app_client.post(ACEPTAR, json={"token": token, "new_password": CLAVE})
    assert bloqueado.status_code == 429
    assert (await _leer_invitacion(invitation_id)).estado == "pendiente"
