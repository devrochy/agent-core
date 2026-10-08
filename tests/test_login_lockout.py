"""HU-ARCH-04: bloqueo progresivo de login — 5 fallos en 10 min → 15 min de
bloqueo con mensaje neutro, por identificador e IP, con aislamiento por
negocio en la API de campo."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.security.login_lockout as lockout


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis caído")
        actual = int(self.kv.get(key, 0)) + 1
        self.kv[key] = str(actual)
        return actual

    async def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl

    async def set(self, key: str, val: str, ex: int | None = None) -> None:
        self.kv[key] = val
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis caído")
        return self.kv.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.kv.pop(key, None)
            self.ttls.pop(key, None)


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(lockout, "_redis", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_cinco_fallos_bloquean_el_sexto(redis) -> None:
    for _ in range(4):
        assert await lockout.register_failure("admin", "ana@x.com", "1.2.3.4") is False
        assert await lockout.is_blocked("admin", "ana@x.com", "1.2.3.4") is False
    assert await lockout.register_failure("admin", "ana@x.com", "1.2.3.4") is True
    assert await lockout.is_blocked("admin", "ana@x.com", "1.2.3.4") is True


@pytest.mark.asyncio
async def test_la_ip_comparte_el_bloqueo(redis) -> None:
    for i in range(5):
        await lockout.register_failure("admin", f"user{i}@x.com", "5.6.7.8")
    assert await lockout.is_blocked("admin", "otro@x.com", "5.6.7.8") is True
    assert await lockout.is_blocked("admin", "otro@x.com", "9.9.9.9") is False


@pytest.mark.asyncio
async def test_scope_aisla_negocios(redis) -> None:
    for _ in range(5):
        await lockout.register_failure("gestor:negocio-a", "pedro", "9.9.9.9")
    assert await lockout.is_blocked("gestor:negocio-a", "pedro", "9.9.9.9") is True
    assert await lockout.is_blocked("gestor:negocio-b", "pedro", "9.9.9.9") is False
    assert await lockout.is_blocked("gestor:negocio-a", None, "9.9.9.9") is True


@pytest.mark.asyncio
async def test_exito_limpia_los_fallos(redis) -> None:
    for _ in range(4):
        await lockout.register_failure("admin", "ana@x.com", "1.1.1.1")
    await lockout.register_success("admin", "ana@x.com", "1.1.1.1")
    for _ in range(4):
        assert await lockout.register_failure("admin", "ana@x.com", "1.1.1.1") is False
    assert await lockout.register_failure("admin", "ana@x.com", "1.1.1.1") is True


@pytest.mark.asyncio
async def test_redis_caido_no_bloquea_el_login(monkeypatch) -> None:
    caido = FakeRedis(fail=True)
    monkeypatch.setattr(lockout, "_redis", lambda: caido)
    assert await lockout.register_failure("admin", "ana@x.com", "1.1.1.1") is False
    assert await lockout.is_blocked("admin", "ana@x.com", "1.1.1.1") is False


class _FakeRequest:
    def __init__(self, headers: dict[str, str], host: str | None = "10.0.0.9") -> None:
        self.headers = headers
        self.client = SimpleNamespace(host=host) if host is not None else None


def test_client_ip_detras_de_un_proxy_de_confianza() -> None:
    # Traefik (10.0.0.9) agregó la IP que vio; lo de la izquierda lo escribió el cliente.
    assert lockout.client_ip(_FakeRequest({"x-forwarded-for": "6.6.6.6, 181.1.2.3"})) == "181.1.2.3"
    # Saltea los saltos de confianza (cloudflared, BFF) hasta la primera IP pública.
    assert (
        lockout.client_ip(_FakeRequest({"x-forwarded-for": "181.1.2.3, 172.18.0.4, 10.0.1.1"}))
        == "181.1.2.3"
    )
    assert lockout.client_ip(_FakeRequest({"cf-connecting-ip": "190.5.5.5"})) == "190.5.5.5"
    assert lockout.client_ip(_FakeRequest({})) == "10.0.0.9"


def test_client_ip_ignora_cabeceras_de_una_conexion_directa() -> None:
    """Sin proxy de confianza delante, las cabeceras las escribe el cliente: con
    ellas podría esquivar el bloqueo (otra IP por intento) o bloquear a otro."""
    req = _FakeRequest(
        {"x-forwarded-for": "181.1.2.3", "cf-connecting-ip": "190.5.5.5"}, host="200.7.7.7"
    )
    assert lockout.client_ip(req) == "200.7.7.7"


def test_client_ip_cadena_rota_no_sigue_leyendo_lo_del_cliente() -> None:
    req = _FakeRequest({"x-forwarded-for": "181.1.2.3, basura, 10.0.0.5"})
    assert lockout.client_ip(req) == "10.0.0.9"


def test_client_ip_respeta_trusted_proxy_cidrs(monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "trusted_proxy_cidrs", "10.0.0.9/32")
    # 10.0.0.5 ya no es de confianza: es el cliente.
    req = _FakeRequest({"x-forwarded-for": "181.1.2.3, 10.0.0.5"})
    assert lockout.client_ip(req) == "10.0.0.5"


def test_client_ip_sin_socket() -> None:
    assert lockout.client_ip(_FakeRequest({}, host=None)) == "desconocida"


# ---------------------------------------------------------------- router /api/v1


def _api_request(headers: dict[str, str] | None = None):
    from fastapi import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {"type": "http", "method": "POST", "path": "/api/v1/auth/login", "headers": raw, "query_string": b""}
    )


@pytest.mark.asyncio
async def test_post_login_bloqueado_rechaza_sin_verificar_credenciales(monkeypatch) -> None:
    import core.api.routers.auth as auth_router
    from core.api.errors import ApiError
    from core.api.routers.auth import LoginRequest, post_login

    async def _bloqueado(scope: str, identifier: str, ip: str) -> bool:
        return True

    async def _no_verificar(*a, **k):  # pragma: no cover
        raise AssertionError("no debe verificar credenciales si está bloqueado")

    monkeypatch.setattr(lockout, "is_blocked", _bloqueado)
    monkeypatch.setattr(auth_router, "login", _no_verificar)

    with pytest.raises(ApiError) as exc:
        await post_login(LoginRequest(identifier="ana@x.com", password="x"), _api_request())
    assert exc.value.status_code == 429
    assert exc.value.code == "auth_bloqueado"


@pytest.mark.asyncio
async def test_post_login_fallido_cuenta_el_fallo(monkeypatch) -> None:
    import core.api.routers.auth as auth_router
    from core.api.errors import ApiError
    from core.api.routers.auth import LoginRequest, post_login

    fallos: list[tuple[str, str, str]] = []

    async def _libre(scope: str, identifier: str, ip: str) -> bool:
        return False

    async def _fallo(scope: str, identifier: str, ip: str) -> bool:
        fallos.append((scope, identifier, ip))
        return False

    async def _login_invalido(*a, **k):
        return None

    monkeypatch.setattr(lockout, "is_blocked", _libre)
    monkeypatch.setattr(lockout, "register_failure", _fallo)
    monkeypatch.setattr(auth_router, "login", _login_invalido)

    with pytest.raises(ApiError) as exc:
        await post_login(LoginRequest(identifier=" Ana@X.com ", password="x"), _api_request())
    assert exc.value.status_code == 401
    assert fallos == [("admin", "ana@x.com", "desconocida")]


@pytest.mark.asyncio
async def test_post_login_exitoso_limpia_los_fallos(monkeypatch) -> None:
    import uuid

    import core.api.routers.auth as auth_router
    from core.api.routers.auth import LoginRequest, post_login

    limpiezas: list[tuple[str, str, str]] = []

    async def _libre(scope: str, identifier: str, ip: str) -> bool:
        return False

    async def _limpia(scope: str, identifier: str, ip: str) -> None:
        limpiezas.append((scope, identifier, ip))

    async def _login_ok(*a, **k):
        return "tok-1"

    async def _admin(*a, **k):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(lockout, "is_blocked", _libre)
    monkeypatch.setattr(lockout, "register_success", _limpia)
    monkeypatch.setattr(auth_router, "login", _login_ok)
    monkeypatch.setattr(auth_router, "admin_from_token", _admin)

    result = await post_login(LoginRequest(identifier="ana@x.com", password="x"), _api_request())
    assert result.token == "tok-1"
    assert limpiezas == [("admin", "ana@x.com", "desconocida")]


# ---------------------------------------------------------------- API de campo


class StubRequest:
    def __init__(self, body: dict) -> None:
        self.body = body
        self.headers: dict[str, str] = {}
        self.client = None

    async def json(self) -> dict:
        return self.body


@pytest.mark.asyncio
async def test_gestor_bloqueado_rechaza_antes_de_resolver_el_tenant(monkeypatch) -> None:
    from pack_cobranza import api_gestor

    async def _bloqueado(scope: str, identifier: str, ip: str) -> bool:
        return True

    async def _no_resolver(*a, **k):  # pragma: no cover
        raise AssertionError("no debe resolver el tenant si el login está bloqueado")

    monkeypatch.setattr(lockout, "is_blocked", _bloqueado)

    import core.tenants

    monkeypatch.setattr(core.tenants, "resolve_tenant_by_slug", _no_resolver)

    respuesta = await api_gestor._login_con_tenant(
        StubRequest({"tenant": "demo", "usuario": "gestor", "password": "x"}), "gestor"
    )
    assert respuesta.status_code == 429
    body = respuesta.body  # JSONResponse guarda el cuerpo en `body`
    assert "demasiados intentos" in str(body)
