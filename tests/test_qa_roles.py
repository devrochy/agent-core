"""Rol tester (SPEC-30, ADR-0134): actúa como master salvo Colaboradores, solo con la variable."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.admin import roles
from core.config import get_settings


@pytest.fixture
def habilitar(monkeypatch):
    def _set(valor: bool) -> None:
        monkeypatch.setattr(get_settings(), "qa_testers_enabled", valor)

    return _set


def _admin(role: str, active: bool = True):
    return SimpleNamespace(role=role, active=active)


def test_master_siempre_escribe(habilitar):
    habilitar(False)
    assert roles.acceso_de_plataforma(_admin("master"), "collaborators") == "write"
    assert roles.actua_como_master(_admin("master"))


def test_tester_habilitado_opera_todo_incluido_colaboradores(habilitar):
    habilitar(True)
    tester = _admin("tester")
    assert roles.actua_como_master(tester)
    assert roles.acceso_de_plataforma(tester, "tenants") == "write"
    assert roles.acceso_de_plataforma(tester, "qa") == "write"
    assert roles.acceso_de_plataforma(tester, "pack_cobranza:carteras") == "write"
    # Colaboradores habilitada (2026-10-13); ahí actúa como dueño, no como master.
    assert roles.acceso_de_plataforma(tester, "collaborators") == "write"
    assert not roles.es_master(tester)
    assert roles.cuenta_habilitada(tester)


def test_tester_sin_la_variable_no_existe(habilitar):
    habilitar(False)
    tester = _admin("tester")
    assert not roles.actua_como_master(tester)
    assert not roles.cuenta_habilitada(tester)
    assert roles.acceso_de_plataforma(tester, "tenants") == "none"


def test_otros_roles_no_los_decide_la_plataforma(habilitar):
    habilitar(True)
    assert roles.acceso_de_plataforma(_admin("colaborador"), "tenants") is None
    assert not roles.actua_como_master(_admin("colaborador"))
    assert roles.cuenta_habilitada(_admin("colaborador"))
    assert not roles.cuenta_habilitada(_admin("colaborador", active=False))
    assert not roles.cuenta_habilitada(None)


async def test_permissions_for_del_tester_incluye_colaboradores(habilitar):
    from core.admin.permissions import effective_access, permissions_for

    habilitar(True)
    tester = _admin("tester")
    mapa = await permissions_for(tester, ["dashboard", "tenants", "qa", "collaborators"])
    assert mapa == {"dashboard": "write", "tenants": "write", "qa": "write", "collaborators": "write"}
    assert await effective_access(tester, "collaborators", None) == "write"
    assert await effective_access(tester, "tenants", None) == "write"
    habilitar(False)
    assert await permissions_for(tester, ["dashboard", "collaborators"]) == {"dashboard": None, "collaborators": None}


async def test_app_de_campo_no_acepta_refresh_ni_cuentas_deshabilitadas(habilitar, monkeypatch):
    import uuid

    import pack_cobranza.api_gestor as api_gestor
    import pack_cobranza.auth as campo_auth

    tenant = uuid.uuid4()
    admin_id = uuid.uuid4()
    payloads = {"refresh": {"typ": "refresh", "admin_id": str(admin_id)}, "access": {"typ": "access", "admin_id": str(admin_id)}}

    async def _token_tenant(token):
        return tenant, payloads[token]

    cuenta = {"admin": SimpleNamespace(role="tester", active=True)}

    class _Sesion:
        async def get(self, _modelo, _id):
            return cuenta["admin"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    negocio = {"activo": True, "vinculado": True}

    async def _is_tenant_active(_tid):
        return negocio["activo"]

    async def _vinculado(_session, _tid, _aid):
        return negocio["vinculado"]

    # Un solo validador para `api.py` y `api_gestor.py` (ADR-0136).
    monkeypatch.setattr(campo_auth, "token_tenant", _token_tenant)
    monkeypatch.setattr(campo_auth, "get_session", lambda: _Sesion())
    monkeypatch.setattr(campo_auth, "is_tenant_active", _is_tenant_active)
    monkeypatch.setattr(campo_auth, "_vinculado", _vinculado)

    habilitar(True)
    assert await api_gestor._auth("Bearer refresh") is None
    assert await api_gestor._auth("Bearer access") is not None
    habilitar(False)  # producción: el tester queda fuera aunque su token no haya vencido
    assert await api_gestor._auth("Bearer access") is None
    habilitar(True)
    cuenta["admin"] = SimpleNamespace(role="colaborador", active=False)  # revocado
    assert await api_gestor._auth("Bearer access") is None
    # ADR-0136: negocio suspendido o cuenta desvinculada → fuera en el siguiente pedido.
    cuenta["admin"] = SimpleNamespace(role="colaborador", active=True)
    assert await api_gestor._auth("Bearer access") is not None
    negocio["vinculado"] = False
    assert await api_gestor._auth("Bearer access") is None
    negocio["vinculado"] = True
    negocio["activo"] = False
    assert await api_gestor._auth("Bearer access") is None
    cuenta["admin"] = SimpleNamespace(role="tester", active=True)
    negocio["activo"], negocio["vinculado"] = True, False  # el tester no necesita vínculo
    assert await api_gestor._auth("Bearer access") is not None
    # La API de dispositivo (`api.py`) usa el mismo validador: desvinculado → fuera.
    import pack_cobranza.api as api_campo

    cuenta["admin"] = SimpleNamespace(role="colaborador", active=True)
    negocio["vinculado"] = True
    assert await api_campo._auth("Bearer access") is not None
    negocio["vinculado"] = False
    assert await api_campo._auth("Bearer access") is None
    assert await api_campo._auth("Bearer refresh") is None
