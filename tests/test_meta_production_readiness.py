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


def test_rls_tests_pass_exit_distinto_de_cero_falla(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = "1 failed"

    monkeypatch.setattr(readiness_mod.subprocess, "run", lambda *a, **k: FakeResult())
    check = check_rls_tests_pass()
    assert check.ok is False


def test_rls_tests_pass_exit_cero_pasa(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "10 passed"

    monkeypatch.setattr(readiness_mod.subprocess, "run", lambda *a, **k: FakeResult())
    check = check_rls_tests_pass()
    assert check.ok is True


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
