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


def test_tester_habilitado_es_master_sin_colaboradores(habilitar):
    habilitar(True)
    tester = _admin("tester")
    assert roles.actua_como_master(tester)
    assert roles.acceso_de_plataforma(tester, "tenants") == "write"
    assert roles.acceso_de_plataforma(tester, "qa") == "write"
    assert roles.acceso_de_plataforma(tester, "pack_cobranza:carteras") == "write"
    assert roles.acceso_de_plataforma(tester, "collaborators") == "none"
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


async def test_permissions_for_del_tester_oculta_colaboradores(habilitar):
    from core.admin.permissions import effective_access, permissions_for

    habilitar(True)
    tester = _admin("tester")
    mapa = await permissions_for(tester, ["dashboard", "tenants", "qa", "collaborators"])
    assert mapa == {"dashboard": "write", "tenants": "write", "qa": "write", "collaborators": None}
    assert await effective_access(tester, "collaborators", None) is None
    assert await effective_access(tester, "tenants", None) == "write"
    habilitar(False)
    assert await permissions_for(tester, ["dashboard", "collaborators"]) == {"dashboard": None, "collaborators": None}
