"""Tests puros de la API `/api/v1`: token Bearer, resolución de tenant y RBAC.

Sin BD ni red: `admin_from_token`, `admin_tenants` y `permissions_for` se
monkeypatchean para probar solo la lógica de `core/api/deps.py` (ADR-0077).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Request

from core.admin.auth import bearer_token
from core.api import deps
from core.api.deps import Principal
from core.api.errors import ApiError
from core.api.pagination import PageParams, build_page
from core.db.models import Admin


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {"type": "http", "method": "GET", "path": "/api/v1/admin/me", "headers": raw, "query_string": b""}
    )


def _admin(role: str = "owner") -> Admin:
    return Admin(
        id=uuid.uuid4(),
        email="a@solvershub.test",
        username=None,
        password_hash="x",
        role=role,
        active=True,
    )


# ------------------------------------------------------------------ bearer_token
def test_bearer_token_valido() -> None:
    assert bearer_token(_request({"authorization": "Bearer abc.123"})) == "abc.123"
    assert bearer_token(_request({"authorization": "bearer abc"})) == "abc"


def test_bearer_token_ausente_o_malformado() -> None:
    assert bearer_token(_request()) is None
    assert bearer_token(_request({"authorization": "Basic abc"})) is None
    assert bearer_token(_request({"authorization": "Bearer"})) is None
    assert bearer_token(_request({"authorization": "Bearer   "})) is None


def test_session_token_prefiere_cookie_sobre_bearer() -> None:
    both = _request({"cookie": "agent_core_session=c", "authorization": "Bearer b"})
    assert deps.session_token(both) == "c"
    assert deps.session_token(_request({"authorization": "Bearer b"})) == "b"
    assert deps.session_token(_request()) is None


# ------------------------------------------------------------------ _resolve_admin
async def test_resolve_admin_sin_token_es_401() -> None:
    with pytest.raises(ApiError) as exc:
        await deps._resolve_admin(_request())
    assert exc.value.status_code == 401
    assert exc.value.code == "unauthorized"


async def test_resolve_admin_lee_cookie_y_bearer(monkeypatch) -> None:
    admin = _admin()
    seen: list[str] = []

    async def fake_admin_from_token(token: str):
        seen.append(token)
        return admin

    monkeypatch.setattr(deps, "admin_from_token", fake_admin_from_token)

    cookie_req = _request({"cookie": "agent_core_session=cookie-token"})
    assert await deps._resolve_admin(cookie_req) is admin
    bearer_req = _request({"authorization": "Bearer header-token"})
    assert await deps._resolve_admin(bearer_req) is admin
    assert seen == ["cookie-token", "header-token"]


# ------------------------------------------------------------------ get_principal
async def _principal_for(request: Request, x_tenant_id: str | None, monkeypatch, tenants):
    admin = _admin("owner")

    async def fake_admin_from_token(_token: str):
        return admin

    async def fake_admin_tenants(_admin_id):
        return tenants

    async def fake_permissions_for(_admin, _sections):
        return {"dashboard": "read"}

    monkeypatch.setattr(deps, "admin_from_token", fake_admin_from_token)
    monkeypatch.setattr(deps, "admin_tenants", fake_admin_tenants)
    monkeypatch.setattr(deps, "permissions_for", fake_permissions_for)

    agen = deps.get_principal(request, x_tenant_id=x_tenant_id)
    principal = await agen.__anext__()
    await agen.aclose()
    return principal


async def test_principal_usa_el_header_de_tenant(monkeypatch) -> None:
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    tenants = [(t1, "Uno", "owner"), (t2, "Dos", "owner")]
    principal = await _principal_for(
        _request({"authorization": "Bearer t"}), str(t2), monkeypatch, tenants
    )
    assert principal.tenant_id == t2
    assert principal.permissions == {"dashboard": "read"}


async def test_principal_sin_header_cae_al_primer_tenant(monkeypatch) -> None:
    t1 = uuid.uuid4()
    principal = await _principal_for(
        _request({"authorization": "Bearer t"}), None, monkeypatch, [(t1, "Uno", "owner")]
    )
    assert principal.tenant_id == t1


async def test_principal_tenant_ajeno_es_403(monkeypatch) -> None:
    tenants = [(uuid.uuid4(), "Uno", "owner")]
    with pytest.raises(ApiError) as exc:
        await _principal_for(
            _request({"authorization": "Bearer t"}), str(uuid.uuid4()), monkeypatch, tenants
        )
    assert exc.value.status_code == 403


async def test_principal_master_sin_tenants(monkeypatch) -> None:
    principal = await _principal_for(_request({"authorization": "Bearer t"}), None, monkeypatch, [])
    assert principal.tenant_id is None


# ------------------------------------------------------------------ require (RBAC)
async def test_require_deniega_sin_acceso(monkeypatch) -> None:
    async def fake_effective_access(_admin, _section, _tenant_id):
        return None

    monkeypatch.setattr(deps, "effective_access", fake_effective_access)
    dependency = deps.require("tenants")
    agen = dependency(Principal(admin=_admin("colaborador"), tenant_id=None, tenants=[], permissions={}))
    with pytest.raises(ApiError) as exc:
        await agen.__anext__()
    assert exc.value.status_code == 403


async def test_require_exige_write(monkeypatch) -> None:
    async def fake_read_only(_admin, _section, _tenant_id):
        return "read"

    monkeypatch.setattr(deps, "effective_access", fake_read_only)
    principal = Principal(admin=_admin("colaborador"), tenant_id=None, tenants=[], permissions={})
    dependency = deps.require("settings", write=True)
    agen = dependency(principal)
    with pytest.raises(ApiError):
        await agen.__anext__()


async def test_require_master_pasa_siempre(monkeypatch) -> None:
    async def fake_denied(_admin, _section, _tenant_id):  # no debería llamarse
        return None

    monkeypatch.setattr(deps, "effective_access", fake_denied)
    master = Principal(admin=_admin("master"), tenant_id=None, tenants=[], permissions={})
    agen = deps.require("tenants")(master)
    assert await agen.__anext__() is master
    await agen.aclose()


# ------------------------------------------------------------------ paginación
def test_build_page() -> None:
    page = build_page(items=[1, 2], total=5, params=PageParams(page=1, page_size=2))
    assert page.items == [1, 2]
    assert page.has_more is True
    last = build_page(items=[5], total=5, params=PageParams(page=3, page_size=2))
    assert last.has_more is False


def test_page_params_offset() -> None:
    assert PageParams(page=1, page_size=20).offset == 0
    assert PageParams(page=3, page_size=20).offset == 40