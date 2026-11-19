"""Cada check parsea defensivamente (`extra="allow"`), `expires_at==0` ->
"no expira", `is_valid=false` -> `ok=False` + `repair_action` correcto."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import core.gateway.meta_health as health_mod
from core.gateway.meta_health import (
    check_billing,
    check_number_registered,
    check_token_valid,
    check_webhook_override,
    check_webhook_subscribed,
    checks_de_salud,
    decide_status,
    merge_health,
    next_status,
)


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = ""
        self.headers = httpx.Headers({})

    def json(self):
        return self._json


class _FakeRedis:
    """`SET NX` en memoria: lo que usa `send_alert_once`."""

    def __init__(self):
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_expires_at_cero_significa_no_expira(monkeypatch):
    response = _FakeResponse({"data": {"is_valid": True, "expires_at": 0}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is True


@pytest.mark.asyncio
async def test_is_valid_false_marca_ok_false_con_repair_reauthorize(monkeypatch):
    response = _FakeResponse({"data": {"is_valid": False}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    async def _noop_alert(*a, **k):
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert", _noop_alert)

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is False
    assert check.repair_action == "reauthorize"


@pytest.mark.asyncio
async def test_token_por_vencer_en_menos_de_7_dias_falla(monkeypatch):
    soon = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    response = _FakeResponse({"data": {"is_valid": True, "expires_at": soon}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    async def _noop_alert(*a, **k):
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert", _noop_alert)

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is False
    assert check.repair_action == "refresh_token"


@pytest.mark.asyncio
async def test_campos_desconocidos_de_debug_token_no_revientan(monkeypatch):
    response = _FakeResponse({"data": {"is_valid": True, "expires_at": 0, "campo_nuevo_no_documentado": "x"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(response))

    check = await check_token_valid("tok", "app-1", "secret", "v21.0")
    assert check.ok is True


@pytest.mark.asyncio
async def test_webhook_subscribed_data_vacio_falla_con_repair(monkeypatch):
    async def _fake_graph_fetch(*a, **k):
        return {"data": []}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_webhook_subscribed("waba-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is False
    assert check.repair_action == "subscribe_waba"


@pytest.mark.asyncio
async def test_number_registered_no_verificado_falla(monkeypatch):
    async def _fake_graph_fetch(*a, **k):
        return {"quality_rating": "GREEN", "code_verification_status": "PENDING"}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is False


@pytest.mark.asyncio
async def test_number_registered_conectado_sin_codigo_pasa(monkeypatch):
    """Número de prueba de Meta (o registrado sin código SMS): `status=CONNECTED` y
    `code_verification_status=NOT_VERIFIED`. Envía de verdad (verificado en test el
    2026-10-08), así que no es un fallo de salud."""

    async def _fake_graph_fetch(*a, **k):
        return {"status": "CONNECTED", "quality_rating": "GREEN", "code_verification_status": "NOT_VERIFIED"}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is True
    assert "NOT_VERIFIED" in check.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("estado", ["DISCONNECTED", "PENDING", "BANNED", "RESTRICTED", "FLAGGED"])
async def test_number_registered_no_conectado_falla_aunque_este_verificado(monkeypatch, estado):
    async def _fake_graph_fetch(*a, **k):
        return {"status": estado, "quality_rating": "GREEN", "code_verification_status": "VERIFIED"}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is False
    assert estado in check.detail


@pytest.mark.asyncio
async def test_number_registered_quality_red_dispara_alerta(monkeypatch):
    alerts = []

    async def _fake_graph_fetch(*a, **k):
        return {"quality_rating": "RED", "code_verification_status": "VERIFIED"}

    async def _fake_alert(title, detail, severity="warning"):
        alerts.append((title, severity))

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr("core.shared.alerts.send_alert", _fake_alert)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: _FakeRedis())

    check = await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is True
    assert alerts == [("Calidad de número en RED", "critical")]


@pytest.mark.asyncio
async def test_webhook_override_sin_override_configurado_pasa(monkeypatch):
    async def _fake_graph_fetch(*a, **k):
        return {"webhook_configuration": {}}

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)

    check = await check_webhook_override("phone-1", "tok", "secret", "v21.0", uuid.uuid4())
    assert check.ok is True


def test_billing_siempre_ok_none():
    from types import SimpleNamespace

    connection = SimpleNamespace(health={})
    check = check_billing(connection)
    assert check.ok is None
    assert check.repair_action == "configure_billing"


def test_decide_status_ok_todos_pasan():
    checks = {"token_valid": {"ok": True}, "templates": {"ok": True}}
    status, reason = decide_status(checks)
    assert status == "connected"
    assert reason is None


def test_decide_status_falla_no_critica_da_degraded():
    checks = {"token_valid": {"ok": True}, "templates": {"ok": False}}
    status, _reason = decide_status(checks)
    assert status == "degraded"


def test_decide_status_falla_critica_da_error():
    checks = {"token_valid": {"ok": False}, "templates": {"ok": True}}
    status, _reason = decide_status(checks)
    assert status == "error"


# --- ADR-0102: solo se mejora de estado con el diagnóstico verificado ---------------

OK = {"ok": True}
FALLA = {"ok": False}
SIN_DATO = {"ok": None}


def _checks(token=OK, app=OK, waba=OK, **otros):
    checks = {"app_subscription_active": app, "billing": SIN_DATO, **otros}
    if token is not None:
        checks["token_valid"] = token
    if waba is not None:
        checks["own_app_subscribed"] = waba
    return checks


@pytest.mark.parametrize(
    ("current", "origin", "checks", "esperado"),
    [
        # Mejora con todo verificado.
        ("error", "byok", _checks(), "connected"),
        ("error", "embedded_signup", _checks(), "connected"),
        ("degraded", "byok", _checks(), "connected"),
        ("error", "byok", _checks(templates=FALLA), "degraded"),
        # Sin token verificado no mejora: falta (sin credenciales) o `None` (Meta no respondió).
        ("error", "byok", _checks(token=None), "error"),
        ("error", "byok", _checks(token=SIN_DATO), "error"),
        ("error", "manual", _checks(token=None), "error"),
        ("degraded", "embedded_signup", _checks(token=SIN_DATO), "degraded"),
        # BYOK además necesita SU app suscrita: la suscripción de la app confirmada y la WABA
        # suscrita a esa app (Embedded Signup y manual, no la WABA).
        ("error", "byok", _checks(app=SIN_DATO), "error"),
        ("degraded", "byok", _checks(app=SIN_DATO), "degraded"),
        ("error", "byok", _checks(waba=SIN_DATO), "error"),
        ("error", "byok", _checks(waba=None), "error"),
        ("degraded", "byok", _checks(waba=SIN_DATO), "degraded"),
        # Embedded Signup necesita la suscripción de la app de la plataforma confirmada (ADR-0128):
        # sin dato no mejora. Manual todavía no (BACKLOG).
        ("error", "embedded_signup", _checks(app=SIN_DATO), "error"),
        ("pending", "embedded_signup", _checks(app=SIN_DATO), "pending"),
        ("error", "manual", _checks(app=SIN_DATO), "connected"),
        ("error", "embedded_signup", _checks(waba=None), "connected"),
        # Empeorar siempre se puede, verificado o no.
        ("connected", "byok", _checks(token=None, templates=FALLA), "degraded"),
        ("connected", "byok", _checks(token=FALLA), "error"),
        ("degraded", "embedded_signup", _checks(token=None, app=FALLA), "error"),
        # Igual que hoy: `connected` sin credenciales sigue `connected`.
        ("connected", "manual", _checks(token=None, app=SIN_DATO), "connected"),
        # Un alta a medias (`pending`) no está verificada: a `connected` solo con el diagnóstico
        # verificado; empeorar, sí.
        ("pending", "byok", _checks(token=None, app=SIN_DATO), "pending"),
        ("pending", "byok", _checks(), "connected"),
        ("pending", "embedded_signup", _checks(token=SIN_DATO), "pending"),
        ("pending", "byok", _checks(token=FALLA), "error"),
        ("pending", "byok", _checks(templates=FALLA), "degraded"),
        # Un estado que no conoce no cambia (falla cerrado): nunca reactiva una `revoked`.
        ("revoked", "byok", _checks(), "revoked"),
        ("draft", "embedded_signup", _checks(token=FALLA), "draft"),
    ],
)
def test_next_status(current, origin, checks, esperado):
    status, _reason = next_status(current, "razón previa", origin, checks)
    assert status == esperado


def test_next_status_si_no_mejora_conserva_la_razon():
    assert next_status("error", "Fallo guardando credenciales", "byok", _checks(token=None)) == (
        "error",
        "Fallo guardando credenciales",
    )


def test_next_status_si_cambia_usa_la_razon_nueva():
    status, reason = next_status("connected", None, "byok", _checks(token=FALLA))
    assert status == "error"
    assert reason == "Checks críticos fallando: token_valid"


def test_merge_health_conserva_lo_que_no_es_un_check():
    previo = {
        "coexistence": True,
        "token_scope_check": {"ok": True},
        "webhook_subscription_active": True,
        "billing": {"ok": None, "manual_status": "al_dia"},
        "token_valid": {"ok": False},
        "number_registered:viejo": {"ok": False},
    }
    checks = {"token_valid": {"ok": True}, "billing": {"ok": None, "detail": "x"}}

    merged = merge_health(previo, checks)

    assert merged == {
        "token_valid": {"ok": True},
        "billing": {"ok": None, "detail": "x", "manual_status": "al_dia"},
        "coexistence": True,
        "token_scope_check": {"ok": True},
        "webhook_subscription_active": True,
    }


def test_merge_health_sin_previo_son_los_checks():
    checks = {"token_valid": {"ok": True}}
    assert merge_health(None, checks) == checks


def test_checks_de_salud_lista_solo_los_checks_en_orden():
    """HU-CONN-04: el panel listaba como chequeos los datos del alta (`coexistence`...)."""
    health = {
        "templates": {"ok": True, "detail": "7 plantillas"},
        "coexistence": True,
        "token_scope_check": {"ok": True, "has_whatsapp_scope": True},
        "webhook_subscription_active": True,
        "billing": {"ok": None, "detail": "Estado manual: al_dia", "manual_status": "al_dia"},
        "number_registered:222": {"ok": True},
        "number_registered:111": {"ok": False, "detail": "status=BANNED"},
        "token_valid": {"ok": False, "detail": "Token inválido", "repair_action": "reauthorize",
                        "checked_at": "2026-10-08T18:00:00+00:00"},
        "algo_nuevo": {"ok": True},
        "sin_ok": {"detail": "no es un check"},
    }

    checks = checks_de_salud(health)

    assert [c["key"] for c in checks] == [
        "token_valid", "number_registered:111", "number_registered:222", "templates", "billing", "algo_nuevo",
    ]
    assert checks[0] == {
        "key": "token_valid", "ok": False, "detail": "Token inválido", "repair_action": "reauthorize",
        "checked_at": "2026-10-08T18:00:00+00:00",
    }


def test_checks_de_salud_sin_salud_es_vacio():
    assert checks_de_salud(None) == []
    assert checks_de_salud({}) == []


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


def _fake_session_con_wabas(wabas):
    """`run_health_checks` lee las WABAs de la conexión y después sus números (ninguno)."""
    from contextlib import asynccontextmanager

    class _Session:
        def __init__(self):
            self._llamadas = 0

        async def execute(self, *_a, **_k):
            self._llamadas += 1
            return _Result(list(wabas) if self._llamadas == 1 else [])

    @asynccontextmanager
    async def _fake_session():
        yield _Session()

    return _fake_session


async def _app_sin_dato(_connection):
    from core.gateway.meta_preflight import PreflightCheck

    return PreflightCheck(key="app_subscription_active", ok=None, detail="x", checked_at=datetime.now(UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "reason", "ok"),
    [
        # BYOK sin credenciales propias nunca es legítimo: falla, y la conexión baja a `error`.
        ("byok", "missing_access_token", False),
        ("byok", "missing_app_secret", False),
        # El resto no se puede revisar: sin dato, no empeora ni deja mejorar.
        ("byok", "no_tenant_scope", None),
        ("embedded_signup", "missing_access_token", None),
        ("manual", "platform_app_not_configured", None),
    ],
)
async def test_run_health_checks_sin_credenciales(monkeypatch, origin, reason, ok):
    """Sin credenciales el token no se puede revisar ni se le pega a Meta; el panel ofrece
    reautorizar."""
    from types import SimpleNamespace

    import core.gateway.meta_credentials as credentials_mod
    from core.gateway.meta_credentials import CredentialsUnavailable

    async def _sin_credenciales(*_a, **_k):
        raise CredentialsUnavailable("No hay token de acceso guardado para esta conexión", reason=reason)

    async def _no_debe_llamarse(*_a, **_k):
        raise AssertionError("sin credenciales no se le pega a Meta")

    monkeypatch.setattr(health_mod, "get_session", _fake_session_con_wabas([]))
    monkeypatch.setattr(credentials_mod, "credentials_for", _sin_credenciales)
    monkeypatch.setattr(health_mod, "check_app_subscription", _app_sin_dato)
    monkeypatch.setattr(health_mod, "check_token_valid", _no_debe_llamarse)

    connection = SimpleNamespace(id=uuid.uuid4(), origin=origin, health={})
    checks = await health_mod.run_health_checks(connection, uuid.uuid4())

    assert checks["token_valid"]["ok"] is ok
    assert "No hay token de acceso guardado" in checks["token_valid"]["detail"]
    assert checks["token_valid"]["repair_action"] == "reauthorize"
    assert next_status("error", None, origin, checks)[0] == "error"
    # Un alta BYOK fallida que el "Diagnosticar" de antes dejó en `connected` baja a `error`.
    assert next_status("connected", None, origin, checks)[0] == ("error" if ok is False else "connected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "suscritas", "esperado", "diagnostico"),
    [
        ("byok", [True], True, "connected"),
        ("byok", [False, True], True, "connected"),  # alguna de sus WABAs
        # WABA sin suscribir a esta app (un reintento que falló al suscribir): `error`, que la
        # deja detrás de la sana, esté `connected` o `degraded`, en el orden del secreto.
        ("byok", [False], False, "error"),
        # Sin WABA cargada (BYOK anteriores a ADR-0036): no degrada, pero no deja mejorar.
        ("byok", [], None, "connected"),
        ("embedded_signup", [False], "falta", "connected"),  # solo BYOK lo necesita
    ],
)
async def test_run_health_checks_own_app_subscribed(monkeypatch, origin, suscritas, esperado, diagnostico):
    """¿La WABA quedó suscrita a la app de ESTA conexión? Sale de `WabaAccount.subscribed`."""
    from types import SimpleNamespace

    import core.gateway.meta_credentials as credentials_mod
    from core.gateway.meta_preflight import PreflightCheck

    async def _creds(*_a, **_k):
        return SimpleNamespace(access_token="tok", app_id="app", app_secret="s", graph_api_version="v21.0")

    async def _ok(*_a, **_k):
        return PreflightCheck(key="x", ok=True, detail="ok", checked_at=datetime.now(UTC))

    wabas = [SimpleNamespace(id=uuid.uuid4(), waba_id=f"waba-{i}", subscribed=sub) for i, sub in enumerate(suscritas)]
    monkeypatch.setattr(health_mod, "get_session", _fake_session_con_wabas(wabas))
    monkeypatch.setattr(credentials_mod, "credentials_for", _creds)
    monkeypatch.setattr(health_mod, "check_app_subscription", _ok)
    monkeypatch.setattr(health_mod, "check_token_valid", _ok)
    monkeypatch.setattr(health_mod, "check_webhook_subscribed", _ok)
    monkeypatch.setattr(health_mod, "check_templates", _ok)

    connection = SimpleNamespace(id=uuid.uuid4(), origin=origin, health={})
    checks = await health_mod.run_health_checks(connection, uuid.uuid4())

    if esperado == "falta":
        assert "own_app_subscribed" not in checks
    else:
        assert checks["own_app_subscribed"]["ok"] is esperado
    assert decide_status(checks)[0] == diagnostico
    # Un reintento que ya estaba en `connected` baja; uno en `error` solo mejora con la prueba.
    assert next_status("connected", None, origin, checks)[0] == diagnostico
    assert next_status("error", None, origin, checks)[0] == (diagnostico if esperado in (True, "falta") else "error")


# --- ADR-0102: las alertas de un cron horario salen una vez al día ------------------


@pytest.mark.asyncio
async def test_send_alert_once_alerta_una_sola_vez(monkeypatch):
    from core.shared import alerts

    enviadas = []

    async def _fake_alert(title, detail, severity="warning"):
        enviadas.append(title)

    redis = _FakeRedis()
    monkeypatch.setattr(alerts, "send_alert", _fake_alert)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)

    await alerts.send_alert_once("k", "Una", "d")
    await alerts.send_alert_once("k", "Una", "d")
    await alerts.send_alert_once("otra", "Otra", "d")

    assert enviadas == ["Una", "Otra"]


@pytest.mark.asyncio
async def test_send_alert_once_sin_redis_alerta_igual(monkeypatch):
    from core.shared import alerts

    enviadas = []

    async def _fake_alert(title, detail, severity="warning"):
        enviadas.append(title)

    class _RedisCaido:
        async def set(self, *_a, **_k):
            raise ConnectionError("redis caído")

    monkeypatch.setattr(alerts, "send_alert", _fake_alert)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: _RedisCaido())

    await alerts.send_alert_once("k", "Una", "d")
    await alerts.send_alert_once("k", "Una", "d")

    assert enviadas == ["Una", "Una"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"is_valid": False},
        {"is_valid": True, "expires_at": int((datetime.now(UTC) + timedelta(days=3)).timestamp())},
    ],
)
async def test_check_token_valid_alerta_una_vez_por_conexion_y_nunca_sin_conexion(monkeypatch, data):
    enviadas = []

    async def _fake_alert(title, detail, severity="warning"):
        enviadas.append((title, detail))

    redis = _FakeRedis()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResponse({"data": data})))
    monkeypatch.setattr("core.shared.alerts.send_alert", _fake_alert)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)
    cid, tid = uuid.uuid4(), uuid.uuid4()

    # Sin conexión (el wizard manual): no alerta, aunque el check falle.
    assert (await check_token_valid("tok", "app-1", "secret", "v21.0")).ok is False
    assert enviadas == []

    for _ in range(3):  # el cron, hora tras hora
        assert (await check_token_valid("tok", "app-1", "s", "v21.0", connection_id=cid, tenant_id=tid)).ok is False
    assert len(enviadas) == 1
    assert str(cid) in enviadas[0][1] and str(tid) in enviadas[0][1]

    await check_token_valid("tok", "app-1", "s", "v21.0", connection_id=uuid.uuid4(), tenant_id=tid)
    assert len(enviadas) == 2, "otra conexión alerta por su cuenta"


@pytest.mark.asyncio
async def test_check_token_valid_por_vencer_no_calla_el_invalido(monkeypatch):
    enviadas = []

    async def _fake_alert(title, detail, severity="warning"):
        enviadas.append(title)

    redis = _FakeRedis()
    monkeypatch.setattr("core.shared.alerts.send_alert", _fake_alert)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)
    cid = uuid.uuid4()

    pronto = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResponse({"data": {"is_valid": True, "expires_at": pronto}}))
    )
    await check_token_valid("tok", "app-1", "s", "v21.0", connection_id=cid, tenant_id=uuid.uuid4())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient(_FakeResponse({"data": {"is_valid": False}})))
    await check_token_valid("tok", "app-1", "s", "v21.0", connection_id=cid, tenant_id=uuid.uuid4())

    assert enviadas == ["Token de Meta por vencer", "Token de Meta inválido"]


@pytest.mark.asyncio
async def test_quality_red_alerta_una_vez_por_numero(monkeypatch):
    enviadas = []

    async def _fake_graph_fetch(*a, **k):
        return {"quality_rating": "RED", "code_verification_status": "VERIFIED"}

    async def _fake_alert(title, detail, severity="warning"):
        enviadas.append(detail)

    redis = _FakeRedis()
    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr("core.shared.alerts.send_alert", _fake_alert)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)

    cid = uuid.uuid4()
    for _ in range(3):
        await check_number_registered("phone-1", "tok", "secret", "v21.0", cid)
    await check_number_registered("phone-2", "tok", "secret", "v21.0", cid)
    # Si el número pasa a otra conexión (otro negocio), el nuevo dueño también se entera.
    await check_number_registered("phone-1", "tok", "secret", "v21.0", uuid.uuid4())

    assert len(enviadas) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        "http://hooks.example.com/meta",  # sin HTTPS
        "https://127.0.0.1/meta",  # loopback
        "https://10.0.0.5/meta",  # red privada
        "https://169.254.169.254/latest/meta-data",  # metadata del host
        "https://redis:6379/",  # nombre interno
    ],
)
async def test_webhook_override_no_consulta_urls_internas_ni_sin_https(monkeypatch, override):
    """El override lo controla el negocio y el checklist de producción lo dispara a demanda
    (HU-CONN-07): nunca se hace el GET a algo que no sea un host público por HTTPS."""

    async def _fake_graph_fetch(*a, **k):
        return {"webhook_configuration": {"override_callback_uri": override}}

    class _Prohibido:
        def __init__(self, *a, **k):
            raise AssertionError("no debe abrir conexiones")

    monkeypatch.setattr(health_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(httpx, "AsyncClient", _Prohibido)

    check = await check_webhook_override("phone-1", "tok", "secret", "v21.0", uuid.uuid4())

    assert check.ok is False
