"""`register_number_step`: PIN autogenerado (nunca aparece en logs ni en la
respuesta), 133005 (PIN mismatch, nunca deregister) y 133008 (demasiados
intentos, bloquea sin reintento automático)."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_onboarding as onboarding_mod
from core.gateway.meta_errors import GraphError
from core.gateway.meta_onboarding import PIN_MISMATCH_CODE, TOO_MANY_GUESSES_CODE, WizardError


class _ScalarsResult:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


class FakeSession:
    def __init__(self, phone):
        self._phone = phone

    async def execute(self, *a, **k):
        return _ScalarsResult(self._phone)

    async def get(self, model, id_):
        return self._phone

    async def flush(self):
        return None


class FakeSessionCM:
    def __init__(self, phone):
        self._phone = phone

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeSession(self._phone)

    async def __aexit__(self, *a):
        return False


def _setup(monkeypatch, *, coexistence=False, locked=False):
    wizard = SimpleNamespace(
        id=uuid.uuid4(), connection_id=uuid.uuid4(), tenant_id=uuid.uuid4(), current_step="register_number",
        channel="whatsapp", environment="production",
        completed_steps=[], context={"register_number_locked": True} if locked else {}, last_error=None,
    )
    connection = SimpleNamespace(business_id="biz-1", graph_api_version="v21.0", health={"coexistence": coexistence})
    phone = SimpleNamespace(id=uuid.uuid4(), phone_number_id="phone-1", registered=False, pin_set=False)

    monkeypatch.setattr(onboarding_mod, "get_current_tenant_id", lambda: wizard.tenant_id)
    monkeypatch.setattr(onboarding_mod, "get_settings", lambda: SimpleNamespace(meta_app_secret="s3cr3t"))

    async def _fake_get_owned(session, model, id_, tenant_id=None):
        return wizard

    monkeypatch.setattr(onboarding_mod, "get_owned", _fake_get_owned)

    async def _fake_load_connection(w, session):
        return connection

    monkeypatch.setattr(onboarding_mod, "_load_connection", _fake_load_connection)
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(phone))

    async def _fake_get_token(*a, **k):
        return "tok-de-conexion"

    monkeypatch.setattr(onboarding_mod.meta_token_store, "get_token", _fake_get_token)

    put_calls = []

    async def _fake_put_token(connection_id, kind, subject_id, token, **kwargs):
        put_calls.append((kind, subject_id, token))

    monkeypatch.setattr(onboarding_mod.meta_token_store, "put_token", _fake_put_token)

    return wizard, connection, phone, put_calls


@pytest.mark.asyncio
async def test_pin_generado_nunca_aparece_en_el_resultado(monkeypatch):
    wizard, _connection, phone, put_calls = _setup(monkeypatch)

    captured = {}

    async def _fake_graph_fetch(path, *, body=None, **kwargs):
        captured["body"] = body
        return {"success": True}

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    result = await onboarding_mod.register_number_step(wizard.id)

    pin = put_calls[0][2]
    assert re.fullmatch(r"\d{6}", pin)
    assert captured["body"]["pin"] == pin
    assert pin not in str(vars(result))
    assert result.current_step == "sync_templates"
    assert phone.registered is True
    assert phone.pin_set is True


@pytest.mark.asyncio
async def test_coexistence_salta_el_paso_sin_llamar_a_meta(monkeypatch):
    wizard, _connection, _phone, put_calls = _setup(monkeypatch, coexistence=True)

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("coexistence no debe llamar a graph_fetch")

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _debe_no_llamarse)

    result = await onboarding_mod.register_number_step(wizard.id)
    assert result.current_step == "sync_templates"
    assert put_calls == []


@pytest.mark.asyncio
async def test_133005_da_las_dos_opciones_y_nunca_llama_deregister(monkeypatch):
    wizard, _connection, _phone, _put_calls = _setup(monkeypatch)

    async def _fake_graph_fetch(path, **kwargs):
        if "deregister" in path:
            raise AssertionError("nunca debe llamarse a deregister -- no existe ese endpoint")
        raise GraphError("PIN mismatch", category="PERMANENT", code=PIN_MISMATCH_CODE)

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    with pytest.raises(WizardError) as exc_info:
        await onboarding_mod.register_number_step(wizard.id)
    message = str(exc_info.value)
    assert "PIN nuevo" in message
    assert "WhatsApp Manager" in message


@pytest.mark.asyncio
async def test_133008_bloquea_segundo_intento_sin_llamar_a_meta_de_nuevo(monkeypatch):
    wizard, _connection, _phone, _put_calls = _setup(monkeypatch)
    calls = {"n": 0}

    async def _fake_graph_fetch(path, **kwargs):
        calls["n"] += 1
        raise GraphError("too many guesses", category="PERMANENT", code=TOO_MANY_GUESSES_CODE)

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    with pytest.raises(WizardError):
        await onboarding_mod.register_number_step(wizard.id)
    assert calls["n"] == 1
    assert wizard.context["register_number_locked"] is True

    # Segundo intento (ej. el admin hace clic de nuevo): NO debe llamar a Meta.
    with pytest.raises(WizardError, match="bloqueó"):
        await onboarding_mod.register_number_step(wizard.id)
    assert calls["n"] == 1, "no debe reintentar automáticamente contra Meta"
