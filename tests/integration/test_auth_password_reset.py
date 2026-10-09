"""Recuperación de contraseña self-service (ADR-0098, HU-APP-02).

Cubre: respuesta genérica sin fuga de cuentas, flujo completo (solicitar →
código por correo → aplicar → login con la nueva clave), uso único del código y
revocación de sesiones previas. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
FORGOT = "/api/v1/auth/forgot-password"
RESET = "/api/v1/auth/reset-password"
LOGIN = "/api/v1/auth/login"


@pytest.fixture(autouse=True)
async def _sin_bloqueos_de_reset():
    """El bloqueo y el cupo del reset viven en Redis por IP: cada test arranca limpio."""
    from core.memory.working import get_redis

    redis = get_redis()
    for patron in ("auth:*:reset:*", "auth:*:reset_pedido:*"):
        for clave in await redis.keys(patron):
            await redis.delete(clave)
    yield


async def _make_admin(email: str, password: str = PW) -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(password), role="colaborador")
        session.add(admin)
        await session.flush()
        return admin.id


def _capturar_correo(monkeypatch):
    """Monkeypatchea el correo de sistema y devuelve la lista de envíos capturados."""
    from core.admin import password_reset_service

    enviados: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> str:
        enviados.append((to, subject, body))
        return "msg-id-test"

    monkeypatch.setattr(password_reset_service, "send_system_email", fake_send)
    return enviados


def _codigo_de(body: str) -> str:
    m = re.search(r"Código: ([A-Z0-9]{8})", body)
    assert m is not None, f"sin código en el correo: {body!r}"
    return m.group(1)


async def test_forgot_no_filtra_cuentas(app_client, monkeypatch) -> None:
    enviados = _capturar_correo(monkeypatch)
    r = await app_client.post(FORGOT, json={"identifier": "nadie@test.local"})
    assert r.status_code == 202
    assert r.text == ""
    assert enviados == []


async def test_flujo_completo_uso_unico_y_revocacion(app_client, monkeypatch) -> None:
    email = f"{uniq('reset')}@test.local"
    await _make_admin(email)
    enviados = _capturar_correo(monkeypatch)

    # Sesión previa para verificar la revocación total al aplicar el reset.
    r = await app_client.post(LOGIN, json={"identifier": email, "password": PW})
    assert r.status_code == 200
    token_viejo = r.json()["token"]

    r = await app_client.post(FORGOT, json={"identifier": email})
    assert r.status_code == 202
    assert len(enviados) == 1 and enviados[0][0] == email
    codigo = _codigo_de(enviados[0][2])

    # Código inválido → 400 genérico.
    r = await app_client.post(RESET, json={"token": "XXXXXXXX", "new_password": "nueva-clave-123"})
    assert r.status_code == 400

    # Código correcto → 204 y queda de un solo uso.
    r = await app_client.post(RESET, json={"token": codigo, "new_password": "nueva-clave-123"})
    assert r.status_code == 204
    r = await app_client.post(RESET, json={"token": codigo, "new_password": "otra-clave-456"})
    assert r.status_code == 400

    # La clave vieja ya no entra; la nueva sí.
    r = await app_client.post(LOGIN, json={"identifier": email, "password": PW})
    assert r.status_code == 401
    r = await app_client.post(LOGIN, json={"identifier": email, "password": "nueva-clave-123"})
    assert r.status_code == 200

    # La sesión anterior quedó revocada.
    r = await app_client.get("/api/v1/admin/me", headers={"Authorization": f"Bearer {token_viejo}"})
    assert r.status_code == 401


async def test_solicitud_repetida_invalida_el_codigo_anterior(app_client, monkeypatch) -> None:
    email = f"{uniq('reset2')}@test.local"
    await _make_admin(email)
    enviados = _capturar_correo(monkeypatch)

    await app_client.post(FORGOT, json={"identifier": email})
    await app_client.post(FORGOT, json={"identifier": email})
    assert len(enviados) == 2
    codigo_viejo = _codigo_de(enviados[0][2])
    codigo_nuevo = _codigo_de(enviados[1][2])

    # El código anterior ya no sirve: la solicitud nueva lo invalidó.
    r = await app_client.post(RESET, json={"token": codigo_viejo, "new_password": "nueva-clave-123"})
    assert r.status_code == 400
    r = await app_client.post(RESET, json={"token": codigo_nuevo, "new_password": "nueva-clave-123"})
    assert r.status_code == 204


async def test_codigo_vencido_es_rechazado(app_client, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from core.admin.password_reset_service import _code_hash, _generar_codigo
    from core.db.models import PasswordResetToken
    from core.db.session import get_session

    email = f"{uniq('reset3')}@test.local"
    admin_id = await _make_admin(email)
    codigo = _generar_codigo()
    async with get_session() as session:
        session.add(
            PasswordResetToken(
                admin_id=admin_id,
                token_hash=_code_hash(codigo),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()

    r = await app_client.post(RESET, json={"token": codigo, "new_password": "nueva-clave-123"})
    assert r.status_code == 400


async def test_reset_con_identificador_busca_solo_en_esa_cuenta(app_client, monkeypatch) -> None:
    """HU-SH-06: con el correo, el código de otra cuenta no sirve aunque sea válido."""
    email_a = f"{uniq('reset-a')}@test.local"
    email_b = f"{uniq('reset-b')}@test.local"
    await _make_admin(email_a)
    await _make_admin(email_b)
    enviados = _capturar_correo(monkeypatch)
    await app_client.post(FORGOT, json={"identifier": email_a})
    codigo_a = _codigo_de(enviados[0][2])

    r = await app_client.post(RESET, json={"token": codigo_a, "new_password": "nueva-clave-123", "identifier": email_b})
    assert r.status_code == 400
    r = await app_client.post(
        RESET, json={"token": codigo_a, "new_password": "nueva-clave-123", "identifier": email_a.upper()}
    )
    assert r.status_code == 204


async def test_reset_tiene_bloqueo_progresivo(app_client, monkeypatch) -> None:
    """HU-SH-06: 5 códigos errados en 10 minutos bloquean (429), aunque después llegue el bueno."""
    email = f"{uniq('reset-bloq')}@test.local"
    await _make_admin(email)
    enviados = _capturar_correo(monkeypatch)
    await app_client.post(FORGOT, json={"identifier": email})
    codigo = _codigo_de(enviados[0][2])

    for _ in range(5):
        r = await app_client.post(RESET, json={"token": "XXXXXXXX", "new_password": "nueva-clave-123", "identifier": email})
        assert r.status_code == 400
    r = await app_client.post(RESET, json={"token": codigo, "new_password": "nueva-clave-123", "identifier": email})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "auth_bloqueado"


async def test_forgot_tiene_cupo_y_responde_siempre_igual(app_client, monkeypatch) -> None:
    """HU-SH-06: más de 3 pedidos del mismo correo en 15 minutos no mandan más correos ni anulan
    el código; la respuesta sigue siendo 202 vacía."""
    email = f"{uniq('reset-cupo')}@test.local"
    await _make_admin(email)
    enviados = _capturar_correo(monkeypatch)
    for _ in range(5):
        r = await app_client.post(FORGOT, json={"identifier": email})
        assert (r.status_code, r.text) == (202, "")
    assert len(enviados) == 3
    # El último código enviado sigue valiendo: los pedidos de más no lo anularon.
    r = await app_client.post(RESET, json={"token": _codigo_de(enviados[-1][2]), "new_password": "nueva-clave-123"})
    assert r.status_code == 204
