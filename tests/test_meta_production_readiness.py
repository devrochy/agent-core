"""Cada check de `meta_production_readiness.py` clasifica correctamente
con datos fake -- handshake ok/mal challenge, 32 vs 16 bytes, hash igual/
distinto/no-configurado, heartbeat viejo/ausente/fresco, DLQ con tamaño
>0, grep de secretos en templates."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import core.gateway.meta_production_readiness as readiness_mod
from core.gateway.meta_production_readiness import (
    check_meta_app_configured,
    check_no_secret_leak_in_templates,
    check_prod_secret_differs_from_dev,
    check_rls_tests_pass,
    check_token_keys_configured,
    check_webhook_handshake,
    check_worker_heartbeat_and_dlq,
)


class _FakeResponse:
    def __init__(self, text, status_code=200, content_type="text/plain; charset=utf-8"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        return self._response


@pytest.mark.asyncio
async def test_handshake_sin_https_falla_sin_llamar(monkeypatch):
    def _prohibido(**_k):
        raise AssertionError("no debe llamar a una URL sin HTTPS")

    monkeypatch.setattr(httpx, "AsyncClient", _prohibido)

    check = await check_webhook_handshake("http://example.test/webhooks/whatsapp", "verify-tok")

    assert check.ok is False
    assert "HTTPS" in check.detail


def _settings_meta(monkeypatch, **valores):
    from core.config import get_settings

    for nombre, valor in valores.items():
        monkeypatch.setattr(get_settings(), nombre, valor)


def test_variables_de_la_app_de_meta_completas(monkeypatch):
    _settings_meta(monkeypatch, meta_app_id="app", meta_app_secret="s3cr3t-no-sale", meta_config_id="cfg")

    check = check_meta_app_configured()

    assert check.ok is True
    assert "s3cr3t-no-sale" not in check.detail


def test_variables_de_la_app_de_meta_nombra_las_que_faltan(monkeypatch):
    _settings_meta(monkeypatch, meta_app_id="app", meta_app_secret="", meta_config_id="")

    check = check_meta_app_configured()

    assert check.ok is False
    assert "META_APP_SECRET" in check.detail and "META_CONFIG_ID" in check.detail
    assert "META_APP_ID" not in check.detail.replace("META_APP_SECRET", "")


@pytest.mark.asyncio
async def test_handshake_correcto(monkeypatch):
    captured = {}

    class _EchoClient(_FakeClient):
        async def get(self, url, params=None):
            captured["params"] = params
            return _FakeResponse(params["hub.challenge"])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _EchoClient(None))

    check = await check_webhook_handshake("https://example.test/webhooks/whatsapp", "verify-tok")
    assert check.ok is True
    assert captured["params"]["hub.verify_token"] == "verify-tok"


@pytest.mark.asyncio
async def test_handshake_challenge_incorrecto_falla(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResponse("challenge-equivocado")))

    check = await check_webhook_handshake("https://example.test/webhooks/whatsapp", "verify-tok")
    assert check.ok is False


@pytest.mark.asyncio
async def test_handshake_content_type_incorrecto_falla(monkeypatch):
    class _Client(_FakeClient):
        async def get(self, url, params=None):
            return _FakeResponse(params["hub.challenge"], content_type="application/json")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client(None))

    check = await check_webhook_handshake("https://example.test/webhooks/whatsapp", "verify-tok")
    assert check.ok is False


def test_prod_secret_no_configurado_da_none(monkeypatch):
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: type("S", (), {"dev_app_secret_hash": ""})())
    check = check_prod_secret_differs_from_dev("cualquier-secreto")
    assert check.ok is None


def test_prod_secret_igual_al_de_dev_falla(monkeypatch):
    secret = "el-mismo-secreto"
    dev_hash = hashlib.sha256(secret.encode()).hexdigest()
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: type("S", (), {"dev_app_secret_hash": dev_hash})())
    check = check_prod_secret_differs_from_dev(secret)
    assert check.ok is False


def test_prod_secret_distinto_pasa(monkeypatch):
    dev_hash = hashlib.sha256(b"secreto-de-dev").hexdigest()
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: type("S", (), {"dev_app_secret_hash": dev_hash})())
    check = check_prod_secret_differs_from_dev("secreto-de-produccion")
    assert check.ok is True


def _fake_settings(**overrides):
    defaults = {"token_master_keys": "{}", "token_active_key_id": ""}
    defaults.update(overrides)
    return type("S", (), defaults)()


def test_token_keys_clave_de_32_bytes_pasa(monkeypatch):
    key_b64 = base64.b64encode(b"0" * 32).decode()
    settings = _fake_settings(token_master_keys=json.dumps({"k1": key_b64}), token_active_key_id="k1")
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: settings)

    check = check_token_keys_configured()
    assert check.ok is True


def test_token_keys_clave_de_16_bytes_falla(monkeypatch):
    key_b64 = base64.b64encode(b"0" * 16).decode()
    settings = _fake_settings(token_master_keys=json.dumps({"k1": key_b64}), token_active_key_id="k1")
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: settings)

    check = check_token_keys_configured()
    assert check.ok is False


def test_token_keys_active_key_id_no_esta_en_las_claves(monkeypatch):
    key_b64 = base64.b64encode(b"0" * 32).decode()
    settings = _fake_settings(token_master_keys=json.dumps({"k1": key_b64}), token_active_key_id="k2")
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: settings)

    check = check_token_keys_configured()
    assert check.ok is False


def test_token_keys_json_invalido_falla(monkeypatch):
    settings = _fake_settings(token_master_keys="no es json", token_active_key_id="k1")
    monkeypatch.setattr(readiness_mod, "get_settings", lambda: settings)

    check = check_token_keys_configured()
    assert check.ok is False


class FakeRedis:
    def __init__(self, heartbeat=None, dlq_size=0):
        self._heartbeat = heartbeat
        self._dlq_size = dlq_size

    async def get(self, key):
        return self._heartbeat

    async def xlen(self, key):
        return self._dlq_size


@pytest.mark.asyncio
async def test_heartbeat_ausente_falla(monkeypatch):
    monkeypatch.setattr("core.memory.working.get_redis", lambda: FakeRedis(heartbeat=None))
    check = await check_worker_heartbeat_and_dlq()
    assert check.ok is False


@pytest.mark.asyncio
async def test_heartbeat_viejo_falla(monkeypatch):
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr("core.memory.working.get_redis", lambda: FakeRedis(heartbeat=old))
    check = await check_worker_heartbeat_and_dlq()
    assert check.ok is False


@pytest.mark.asyncio
async def test_heartbeat_fresco_y_dlq_vacio_pasa(monkeypatch):
    fresh = datetime.now(UTC).isoformat()
    monkeypatch.setattr("core.memory.working.get_redis", lambda: FakeRedis(heartbeat=fresh, dlq_size=0))
    check = await check_worker_heartbeat_and_dlq()
    assert check.ok is True


@pytest.mark.asyncio
async def test_dlq_con_eventos_pendientes_falla(monkeypatch):
    fresh = datetime.now(UTC).isoformat()
    monkeypatch.setattr("core.memory.working.get_redis", lambda: FakeRedis(heartbeat=fresh, dlq_size=3))
    check = await check_worker_heartbeat_and_dlq()
    assert check.ok is False


def test_rls_tests_pass_no_corre_pytest_en_el_contenedor(monkeypatch):
    """La imagen desplegada no trae pytest ni tests/, y los tests de integración crean
    negocios en la base configurada: correrlos en un entorno real ensuciaría datos.
    El check informa (ok=None) dónde corren: en CI."""

    def _prohibido(*args, **kwargs):
        raise AssertionError("no debe lanzar subprocesos")

    monkeypatch.setattr("subprocess.run", _prohibido)
    monkeypatch.setenv("SOURCE_COMMIT", "a2c60a3d1b11d160b61fd6caf0d2c9914c5dd1d6")
    monkeypatch.setenv("COOLIFY_BRANCH", "develop")
    check = check_rls_tests_pass()
    assert check.ok is None
    assert "CI" in check.detail
    assert "a2c60a3" in check.detail and "develop" in check.detail


def test_rls_tests_pass_sin_commit_conocido(monkeypatch):
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    monkeypatch.delenv("COOLIFY_BRANCH", raising=False)
    check = check_rls_tests_pass()
    assert check.ok is None
    assert "CI" in check.detail


def test_no_secret_leak_detecta_interpolacion_sospechosa(tmp_path, monkeypatch):
    templates_dir = tmp_path / "core" / "admin" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "malo.html").write_text("<p>{{ connection.app_secret }}</p>")
    static_dir = tmp_path / "core" / "admin" / "static"
    static_dir.mkdir(parents=True)

    monkeypatch.setattr(readiness_mod, "ROOT", tmp_path)
    check = check_no_secret_leak_in_templates()
    assert check.ok is False


def test_no_secret_leak_limpio_pasa(tmp_path, monkeypatch):
    templates_dir = tmp_path / "core" / "admin" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "bueno.html").write_text("<p>{{ connection.channel }}</p>")
    static_dir = tmp_path / "core" / "admin" / "static"
    static_dir.mkdir(parents=True)

    monkeypatch.setattr(readiness_mod, "ROOT", tmp_path)
    check = check_no_secret_leak_in_templates()
    assert check.ok is True


def test_no_secret_leak_no_confunde_conteo_de_tokens_llm_con_un_secreto(tmp_path, monkeypatch):
    """`row.tokens_in`/`row.tokens_out` (conteo de tokens de LLM en
    costs.html) no debe disparar un falso positivo solo porque contienen
    la palabra "token" como substring."""
    templates_dir = tmp_path / "core" / "admin" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "costos.html").write_text("<td>{{ row.tokens_in }}</td><td>{{ row.tokens_out }}</td>")
    static_dir = tmp_path / "core" / "admin" / "static"
    static_dir.mkdir(parents=True)

    monkeypatch.setattr(readiness_mod, "ROOT", tmp_path)
    check = check_no_secret_leak_in_templates()
    assert check.ok is True


def test_no_secret_leak_detecta_access_token_con_guion_bajo(tmp_path, monkeypatch):
    templates_dir = tmp_path / "core" / "admin" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "malo.html").write_text("<p>{{ connection.access_token }}</p>")
    static_dir = tmp_path / "core" / "admin" / "static"
    static_dir.mkdir(parents=True)

    monkeypatch.setattr(readiness_mod, "ROOT", tmp_path)
    check = check_no_secret_leak_in_templates()
    assert check.ok is False


# ------------------------------------------------------------------ HU-CONN-07
def _sub(object_, active=True, fields=None):
    from types import SimpleNamespace

    from core.gateway.meta_subscriptions import OBJECT_FIELDS

    return SimpleNamespace(object=object_, active=active, fields=OBJECT_FIELDS[object_] if fields is None else fields)


def _fake_subscriptions(monkeypatch, rows):
    async def _list(meta_app_id=None):
        return rows

    monkeypatch.setattr("core.gateway.meta_subscriptions.list_app_subscriptions", _list)


@pytest.mark.asyncio
async def test_app_subscriptions_solo_whatsapp_pasa_sin_page_ni_instagram(monkeypatch):
    """Una app que solo usa WhatsApp pasa: `page` e `instagram` no son obligatorios."""
    from core.gateway.meta_production_readiness import check_app_subscriptions_complete

    _fake_subscriptions(monkeypatch, [_sub("whatsapp_business_account")])

    check = await check_app_subscriptions_complete("app-1")

    assert check.ok is True
    assert "page" in check.detail and "instagram" in check.detail  # se informan, no fallan


@pytest.mark.asyncio
async def test_app_subscriptions_whatsapp_inactiva_falla(monkeypatch):
    from core.gateway.meta_production_readiness import check_app_subscriptions_complete

    _fake_subscriptions(
        monkeypatch, [_sub("whatsapp_business_account", active=False), _sub("page"), _sub("instagram")]
    )

    check = await check_app_subscriptions_complete("app-1")

    assert check.ok is False
    assert "whatsapp_business_account" in check.detail


@pytest.mark.asyncio
async def test_app_subscriptions_whatsapp_sin_fields_falla(monkeypatch):
    from core.gateway.meta_production_readiness import check_app_subscriptions_complete

    _fake_subscriptions(monkeypatch, [_sub("whatsapp_business_account", fields=["messages"])])

    check = await check_app_subscriptions_complete("app-1")

    assert check.ok is False
    assert "faltan fields" in check.detail


@pytest.mark.asyncio
async def test_app_subscriptions_sin_app_configurada_falla(monkeypatch):
    from core.gateway.meta_production_readiness import check_app_subscriptions_complete

    check = await check_app_subscriptions_complete("")

    assert check.ok is False


@pytest.mark.asyncio
async def test_checklist_de_plataforma_sin_pytest_ni_subprocesos_ni_checks_de_negocio(monkeypatch):
    """La corrida de plataforma no lanza procesos y no incluye la salud de conexiones de un
    negocio (eso es el checklist por conexión)."""
    from core.gateway.meta_production_readiness import PLATFORM_CHECK_LABELS, run_platform_checks

    def _prohibido(*args, **kwargs):
        raise AssertionError("no debe lanzar subprocesos")

    monkeypatch.setattr("subprocess.run", _prohibido)
    monkeypatch.setattr("subprocess.Popen", _prohibido)
    _fake_subscriptions(monkeypatch, [_sub("whatsapp_business_account")])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResponse("otro")))
    monkeypatch.setattr("core.memory.working.get_redis", lambda: FakeRedis(heartbeat=None))

    async def _rls_ok():
        return readiness_mod.PreflightCheck(key="rls_forced", ok=True, detail="ok", checked_at=datetime.now(UTC))

    monkeypatch.setattr(readiness_mod, "check_rls_forced", _rls_ok)
    monkeypatch.setenv("META_APP_ID", "app-plataforma")
    from core.config import get_settings

    get_settings.cache_clear()
    try:
        checks = await run_platform_checks()
    finally:
        monkeypatch.delenv("META_APP_ID")
        get_settings.cache_clear()

    assert [c.key for c in checks] == list(PLATFORM_CHECK_LABELS)
    assert "connections_health" not in {c.key for c in checks}
    assert next(c for c in checks if c.key == "app_subscriptions_complete").ok is True


@pytest.mark.asyncio
async def test_un_check_que_revienta_queda_sin_evaluar_y_no_tumba_la_corrida(monkeypatch):
    from core.gateway.meta_production_readiness import run_platform_checks

    async def _boom(*a, **k):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(readiness_mod, "check_rls_forced", _boom)
    monkeypatch.setattr(readiness_mod, "check_webhook_handshake", _boom)
    monkeypatch.setattr(readiness_mod, "check_app_subscriptions_complete", _boom)
    monkeypatch.setattr(readiness_mod, "check_worker_heartbeat_and_dlq", _boom)

    checks = {c.key: c for c in await run_platform_checks()}

    assert checks["rls_forced"].ok is None
    assert "RuntimeError" in checks["rls_forced"].detail
    assert "se cayó" not in checks["rls_forced"].detail  # el mensaje crudo no se expone
    assert checks["token_keys_configured"].ok is not None


def _pc(key, ok, detail="d", repair=None):
    return readiness_mod.PreflightCheck(key=key, ok=ok, detail=detail, checked_at=datetime.now(UTC), repair_action=repair)


def test_combinar_checks_falla_si_alguno_falla():
    from core.gateway.meta_production_readiness import combine_checks

    item = combine_checks("webhook", [_pc("a", True, "bien"), _pc("b", None, "sin dato"), _pc("c", False, "mal", "x")])

    assert item.ok is False
    assert item.detail == "mal"
    assert item.repair_action == "x"


def test_combinar_checks_sin_dato_si_ninguno_falla_pero_alguno_no_se_evaluo():
    from core.gateway.meta_production_readiness import combine_checks

    item = combine_checks("webhook", [_pc("a", True), _pc("b", None, "todavía no corrió")])

    assert item.ok is None
    assert item.detail == "todavía no corrió"


def test_combinar_checks_pasa_con_todos_en_verde():
    from core.gateway.meta_production_readiness import combine_checks

    assert combine_checks("token", [_pc("a", True), _pc("b", True)]).ok is True
    assert combine_checks("token", []).ok is None


def test_items_por_canal():
    from core.gateway.meta_production_readiness import connection_item_keys

    assert connection_item_keys("whatsapp") == [
        "token", "number_registered", "waba_subscribed", "webhook", "templates_approved",
    ]
    assert connection_item_keys("messenger") == ["token", "webhook"]
