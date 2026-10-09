"""`register_number_step` (HU-SH-05): el PIN lo escribe el negocio (antes se generaba y nadie
lo veía), el registro pasa por el mismo servicio que "Números" y un rechazo deja el wizard en
el paso con el motivo; `set_new_pin_step` fija el PIN nuevo en Meta y después registra."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.admin.meta_numbers_service as numbers_mod
import core.gateway.meta_onboarding as onboarding_mod
from core.gateway.meta_errors import GraphError
from core.gateway.meta_onboarding import WizardError


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


def _setup(monkeypatch, *, coexistence=False, pin_guardado=None, error_registro=None):
    wizard = SimpleNamespace(
        id=uuid.uuid4(), connection_id=uuid.uuid4(), tenant_id=uuid.uuid4(), current_step="register_number",
        channel="whatsapp", environment="production", completed_steps=[], context={}, last_error=None,
    )
    connection = SimpleNamespace(business_id="biz-1", graph_api_version="v21.0", health={"coexistence": coexistence})
    phone = SimpleNamespace(id=uuid.uuid4(), phone_number_id="phone-1", registered=False, pin_set=False)

    monkeypatch.setattr(onboarding_mod, "get_current_tenant_id", lambda: wizard.tenant_id)
    monkeypatch.setattr(
        onboarding_mod, "get_settings", lambda: SimpleNamespace(meta_app_secret="s3cr3t", graph_api_version="v21.0")
    )

    async def _fake_get_owned(session, model, id_, tenant_id=None):
        return wizard

    monkeypatch.setattr(onboarding_mod, "get_owned", _fake_get_owned)

    async def _fake_load_connection(w, session):
        return connection

    async def _fake_numero_vivo(session, w):
        return phone

    async def _fake_token_de(conn):
        return "tok-de-conexion"

    monkeypatch.setattr(onboarding_mod, "_load_connection", _fake_load_connection)
    monkeypatch.setattr(onboarding_mod, "_numero_vivo", _fake_numero_vivo)
    monkeypatch.setattr(onboarding_mod, "_token_de", _fake_token_de)
    monkeypatch.setattr(onboarding_mod, "get_session", FakeSessionCM(phone))

    async def _fake_get_token(connection_id, kind, subject_id, **kwargs):
        return pin_guardado if kind == onboarding_mod.PIN_KIND else "tok-de-conexion"

    monkeypatch.setattr(onboarding_mod.meta_token_store, "get_token", _fake_get_token)

    put_calls: list = []

    async def _fake_put_token(connection_id, kind, subject_id, token, **kwargs):
        put_calls.append((kind, subject_id, token))

    monkeypatch.setattr(onboarding_mod.meta_token_store, "put_token", _fake_put_token)

    registros: list = []

    async def _fake_register_number(tenant_id, connection_id, phone_number_id, *, pin=None):
        registros.append((tenant_id, connection_id, phone_number_id, pin))
        return error_registro

    monkeypatch.setattr(numbers_mod, "register_number", _fake_register_number)

    esperas: list = []

    async def _fake_esperar_registro(wizard_id, tenant_id, message):
        esperas.append(message)

    monkeypatch.setattr(onboarding_mod, "_esperar_registro", _fake_esperar_registro)

    return wizard, phone, put_calls, registros, esperas


def _redis_libre(monkeypatch):
    import core.memory.working as working_mod

    class _Redis:
        async def set(self, *a, **k):
            return True

    monkeypatch.setattr(working_mod, "get_redis", lambda: _Redis())


@pytest.mark.asyncio
async def test_sin_pin_ni_guardado_no_llama_a_meta_y_lo_pide(monkeypatch):
    wizard, _phone, _put, registros, esperas = _setup(monkeypatch)

    with pytest.raises(WizardError) as exc_info:
        await onboarding_mod.register_number_step(wizard.id)

    assert exc_info.value.code == "pin_required"
    assert registros == []
    assert esperas, "la conexión queda esperando el registro con el motivo"


@pytest.mark.asyncio
async def test_con_el_pin_del_negocio_registra_y_avanza_sin_mostrarlo(monkeypatch):
    wizard, _phone, _put, registros, _esperas = _setup(monkeypatch)

    result = await onboarding_mod.register_number_step(wizard.id, "246810")

    assert registros == [(wizard.tenant_id, str(wizard.connection_id), "phone-1", "246810")]
    assert result.current_step == "sync_templates"
    assert "246810" not in str(vars(result))


@pytest.mark.asyncio
async def test_con_un_pin_ya_guardado_registra_con_ese(monkeypatch):
    wizard, _phone, _put, registros, _esperas = _setup(monkeypatch, pin_guardado="111222")

    await onboarding_mod.register_number_step(wizard.id)

    assert registros[0][3] is None  # el servicio usa el guardado


@pytest.mark.asyncio
async def test_un_pin_mal_formado_no_llega_a_meta(monkeypatch):
    wizard, _phone, _put, registros, _esperas = _setup(monkeypatch)

    with pytest.raises(WizardError) as exc_info:
        await onboarding_mod.register_number_step(wizard.id, "12ab56")

    assert exc_info.value.code == "invalid_pin"
    assert registros == []


@pytest.mark.asyncio
async def test_si_meta_rechaza_el_pin_el_wizard_queda_en_el_paso_con_el_motivo(monkeypatch):
    motivo = "El número ya tiene verificación en dos pasos con otro PIN: escribí ese PIN, o cambialo en WhatsApp Manager"
    wizard, _phone, _put, _registros, esperas = _setup(monkeypatch, error_registro=motivo)

    with pytest.raises(WizardError) as exc_info:
        await onboarding_mod.register_number_step(wizard.id, "246810")

    assert exc_info.value.code == "register_failed"
    assert "WhatsApp Manager" in exc_info.value.message
    assert esperas == [motivo]
    assert wizard.current_step == "register_number"


@pytest.mark.asyncio
async def test_coexistence_salta_el_paso_sin_llamar_a_meta(monkeypatch):
    wizard, _phone, put_calls, registros, _esperas = _setup(monkeypatch, coexistence=True)

    result = await onboarding_mod.register_number_step(wizard.id)

    assert result.current_step == "sync_templates"
    assert registros == [] and put_calls == []


@pytest.mark.asyncio
async def test_fuera_del_paso_de_registro_responde_409(monkeypatch):
    wizard, _phone, _put, registros, _esperas = _setup(monkeypatch)
    wizard.current_step = "sync_templates"

    with pytest.raises(WizardError) as exc_info:
        await onboarding_mod.register_number_step(wizard.id, "246810")

    assert exc_info.value.status == 409
    assert registros == []


@pytest.mark.asyncio
async def test_pin_nuevo_se_fija_en_meta_y_despues_registra(monkeypatch):
    wizard, _phone, put_calls, registros, _esperas = _setup(monkeypatch)
    llamadas: list = []

    async def _fake_graph_fetch(path, *, method="GET", body=None, **kwargs):
        if "deregister" in path:
            raise AssertionError("nunca debe llamarse a deregister")
        llamadas.append((method, path, body))
        return {"success": True}

    liberados: list = []

    async def _fake_release(tenant_id, phone_number_id):
        liberados.append(phone_number_id)

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(numbers_mod, "release_register_rate_limit", _fake_release)
    _redis_libre(monkeypatch)

    result = await onboarding_mod.set_new_pin_step(wizard.id, "975310")

    assert llamadas == [("POST", "/phone-1", {"pin": "975310"})]
    assert (onboarding_mod.PIN_KIND, "phone-1", "975310") in put_calls
    assert liberados == ["phone-1"]
    assert registros[0][3] == "975310"
    assert result.current_step == "sync_templates"


@pytest.mark.asyncio
async def test_pin_nuevo_rechazado_por_meta_no_registra(monkeypatch):
    wizard, _phone, put_calls, registros, _esperas = _setup(monkeypatch)

    async def _fake_graph_fetch(path, **kwargs):
        raise GraphError("Invalid parameter", category="PERMANENT", code=100)

    monkeypatch.setattr(onboarding_mod, "graph_fetch", _fake_graph_fetch)

    async def _fake_record(*a, **k):
        return None

    monkeypatch.setattr(onboarding_mod, "_record_graph_error", _fake_record)
    _redis_libre(monkeypatch)

    with pytest.raises(WizardError) as exc_info:
        await onboarding_mod.set_new_pin_step(wizard.id, "975310")

    assert exc_info.value.code == "set_pin_failed"
    assert registros == [] and put_calls == []


@pytest.mark.asyncio
async def test_pin_nuevo_mal_formado_se_rechaza(monkeypatch):
    wizard, *_ = _setup(monkeypatch)

    with pytest.raises(WizardError):
        await onboarding_mod.set_new_pin_step(wizard.id, "123")
