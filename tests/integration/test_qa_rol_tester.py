"""Rol tester (SPEC-30, HU-QA-04, ADR-0134): invitación con enlace, master sin
Colaboradores, cuentas protegidas de los owners, revocación inmediata y solo en test."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers

pytestmark = [
    pytest.mark.aislamiento,  # permisos y gestión de cuentas: subconjunto de las PRs (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-del-tester-123"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}@example.test"


@pytest.fixture
def testers_on(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)

    async def _sin_correo(to: str, subject: str, body: str) -> None:
        return None

    import core.gateway.system_email as system_email

    monkeypatch.setattr(system_email, "send_system_email", _sin_correo)
    return monkeypatch


async def _invitar_y_aceptar(app_client, master: dict[str, str], email: str) -> tuple[str, dict]:
    r = await app_client.post("/api/v1/admin/qa/testers", json={"email": email}, headers=master)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tester"]["estado"] == "invitado"
    token = body["enlace"].split("#", 1)[1]
    r = await app_client.post(
        "/api/v1/auth/accept-tester-invitation", json={"token": token, "new_password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return token, body["tester"]


async def test_tester_invitado_es_master_sin_colaboradores(app_client, tenant_setup, testers_on) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _uniq("tester")
    token, tester = await _invitar_y_aceptar(app_client, master, email)

    # El enlace es de un solo uso.
    r = await app_client.post(
        "/api/v1/auth/accept-tester-invitation", json={"token": token, "new_password": "otra-clave-123"}
    )
    assert r.status_code == 400

    headers = await api_headers(app_client, t1, email=email, password=PASSWORD)
    r = await app_client.get("/api/v1/admin/me", headers=headers)
    me = r.json()
    assert me["role"] == "tester" and me["qa_testers_habilitados"] is True
    assert me["permissions"]["collaborators"] is None
    assert me["permissions"]["tenants"] == "write" and me["permissions"]["qa"] == "write"
    assert me["tenant_count"] >= 2  # ve todos los negocios, como un master

    # Opera como master: negocios de la plataforma.
    r = await app_client.get("/api/v1/admin/platform/tenants", headers=headers)
    assert r.status_code == 200

    # Pero no gestiona cuentas: colaboradores, invitaciones a negocios ni testers.
    for metodo, ruta, cuerpo in (
        ("get", "/api/v1/admin/collaborators", None),
        ("post", "/api/v1/admin/collaborators", {"email": _uniq("x"), "password": "clave-12345", "role": "colaborador"}),
        ("post", "/api/v1/admin/invitations", {"identifier": _uniq("y"), "role": "colaborador"}),
        ("get", "/api/v1/admin/qa/testers", None),
        ("post", "/api/v1/admin/qa/testers", {"email": _uniq("z")}),
    ):
        kwargs = {"headers": headers}
        if cuerpo is not None:
            kwargs["json"] = cuerpo
        r = await getattr(app_client, metodo)(ruta, **kwargs)
        assert r.status_code == 403, f"{metodo} {ruta}: {r.status_code} {r.text}"

    # El master lo ve en la lista.
    r = await app_client.get("/api/v1/admin/qa/testers", headers=master)
    fila = next(t for t in r.json()["testers"] if t["id"] == tester["id"])
    assert fila["estado"] == "activo"


async def test_owner_no_toca_a_un_tester(app_client, tenant_setup, testers_on) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _uniq("tester-prot")
    _, tester = await _invitar_y_aceptar(app_client, master, email)

    owner_email = _uniq("owner")
    async with get_session() as session:
        owner = Admin(email=owner_email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(owner)
        await session.flush()
        session.add(TenantUser(tenant_id=t1, admin_id=owner.id, role="owner"))
    owner_headers = await api_headers(app_client, t1, email=owner_email, password=PASSWORD)

    for accion, cuerpo in (("toggle", None), ("password", {"password": "hackeada-123"})):
        kwargs = {"headers": owner_headers}
        if cuerpo is not None:
            kwargs["json"] = cuerpo
        r = await app_client.post(f"/api/v1/admin/collaborators/{tester['id']}/{accion}", **kwargs)
        assert r.status_code >= 400, f"{accion}: {r.status_code}"
    # Invitarlo a su negocio tampoco.
    r = await app_client.post(
        "/api/v1/admin/collaborators",
        json={"email": email, "password": "clave-12345", "role": "colaborador"},
        headers=owner_headers,
    )
    assert r.status_code >= 400

    # El tester sigue entrando con su contraseña.
    await api_headers(app_client, t1, email=email, password=PASSWORD)


async def test_revocar_corta_la_sesion_y_reinvitar_la_devuelve(app_client, tenant_setup, testers_on) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _uniq("tester-rev")
    _, tester = await _invitar_y_aceptar(app_client, master, email)
    headers = await api_headers(app_client, t1, email=email, password=PASSWORD)

    r = await app_client.post(f"/api/v1/admin/qa/testers/{tester['id']}/revoke", headers=master)
    assert r.status_code == 200 and r.json()["estado"] == "revocado"
    r = await app_client.get("/api/v1/admin/me", headers=headers)
    assert r.status_code == 401
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PASSWORD})
    assert r.status_code == 401

    # Reinvitar: define una contraseña nueva con otro enlace.
    await _invitar_y_aceptar(app_client, master, email)
    await api_headers(app_client, t1, email=email, password=PASSWORD)


async def test_no_convierte_una_cuenta_existente_en_tester(app_client, tenant_setup, testers_on) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    existente = _uniq("colaborador")
    async with get_session() as session:
        session.add(Admin(email=existente, password_hash=hash_password(PASSWORD), role="colaborador"))
    r = await app_client.post("/api/v1/admin/qa/testers", json={"email": existente}, headers=master)
    assert r.status_code == 409
    r = await app_client.post("/api/v1/admin/qa/testers", json={"email": "no-es-correo"}, headers=master)
    assert r.status_code == 422


async def test_sin_la_variable_el_tester_no_existe(app_client, tenant_setup, monkeypatch) -> None:
    from core.config import get_settings

    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)

    async def _sin_correo(to: str, subject: str, body: str) -> None:
        return None

    import core.gateway.system_email as system_email

    monkeypatch.setattr(system_email, "send_system_email", _sin_correo)
    email = _uniq("tester-off")
    _, _tester = await _invitar_y_aceptar(app_client, master, email)
    headers = await api_headers(app_client, t1, email=email, password=PASSWORD)
    r = await app_client.post("/api/v1/admin/qa/testers", json={"email": _uniq("pendiente")}, headers=master)
    token_pendiente = r.json()["enlace"].split("#", 1)[1]

    # Producción: la variable apagada.
    monkeypatch.setattr(get_settings(), "qa_testers_enabled", False)
    r = await app_client.get("/api/v1/admin/me", headers=headers)
    assert r.status_code == 401  # la sesión que tenía deja de valer
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PASSWORD})
    assert r.status_code == 401
    r = await app_client.post(
        "/api/v1/auth/accept-tester-invitation", json={"token": token_pendiente, "new_password": PASSWORD}
    )
    assert r.status_code == 400
    r = await app_client.post("/api/v1/admin/qa/testers", json={"email": _uniq("nuevo")}, headers=master)
    assert r.status_code == 409
    r = await app_client.get("/api/v1/admin/me", headers=master)
    assert r.json()["qa_testers_habilitados"] is False


async def test_tester_no_gestiona_cuentas_por_el_alta_ni_purga(app_client, tenant_setup, testers_on) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    email = _uniq("tester-alta")
    await _invitar_y_aceptar(app_client, master, email)
    headers = await api_headers(app_client, t1, email=email, password=PASSWORD)

    # Reusar el slug de un negocio existente vincularía un owner títere: 409.
    r = await app_client.post(
        "/api/v1/admin/platform/tenants",
        json={"name": "Alpha", "slug": "test-alpha", "admin_email": _uniq("titere"), "admin_password": "Passw0rd!"},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    # Vincular como owner una cuenta que ya existe: 409.
    r = await app_client.post(
        "/api/v1/admin/platform/tenants",
        json={"name": "Nuevo", "slug": f"qa-{uuid.uuid4().hex[:6]}", "admin_email": "admin@local", "admin_password": "Passw0rd!"},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    # Un negocio nuevo con una cuenta nueva sí (probar el alta es operar).
    slug = f"qa-{uuid.uuid4().hex[:6]}"
    r = await app_client.post(
        "/api/v1/admin/platform/tenants",
        json={"name": "Negocio QA", "slug": slug, "admin_email": _uniq("owner-qa"), "admin_password": "Passw0rd!"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    tenant_id = r.json()["tenant_id"]

    # Purgar es solo del master, también en test (invariante 8).
    r = await app_client.post(
        f"/api/v1/admin/platform/tenants/{tenant_id}/purge",
        json={"confirm_phrase": f"ELIMINAR {slug}"},
        headers=headers,
    )
    assert r.status_code == 403


async def test_checklist_de_produccion_es_del_master(app_client, tenant_setup, testers_on) -> None:
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    catalogo = (
        "version: 99\nfecha: '2026-10-13'\nitems:\n"
        "- {id: prod-qa-rol, seccion: prod, grupo: Seguridad, titulo: Rol apagado, porque: p, como: c}\n"
        "- {id: test-qa-var, seccion: test, grupo: Variables, titulo: Variable, porque: p, como: c}\n"
        "- {id: pruebas-qa-x, seccion: pruebas, grupo: G, titulo: X, pasos: [Hacer], esperado: Pasa}\n"
    )
    r = await app_client.post("/api/v1/admin/qa/catalog/import", json={"contenido": catalogo}, headers=master)
    assert r.status_code == 200, r.text
    email = _uniq("tester-prod")
    await _invitar_y_aceptar(app_client, master, email)
    headers = await api_headers(app_client, t1, email=email, password=PASSWORD)

    r = await app_client.put("/api/v1/admin/qa/config/prod-qa-rol", json={"estado": "listo"}, headers=headers)
    assert r.status_code == 403
    r = await app_client.put("/api/v1/admin/qa/config/test-qa-var", json={"estado": "listo"}, headers=headers)
    assert r.status_code == 200
    r = await app_client.put("/api/v1/admin/qa/config/prod-qa-rol", json={"estado": "en_curso"}, headers=master)
    assert r.status_code == 200
    # El tester cierra el ciclo (actúa como master); no importa el catálogo.
    r = await app_client.post("/api/v1/admin/qa/cycles/close", json={"nombre": "Ciclo del tester"}, headers=headers)
    assert r.status_code == 200, r.text
    r = await app_client.post("/api/v1/admin/qa/catalog/import", json={"contenido": catalogo}, headers=headers)
    assert r.status_code == 403
