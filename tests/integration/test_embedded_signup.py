"""Embedded Signup real en el panel nuevo (HU-SH-05, ADR-0080).

Con la BD y Redis reales; Graph y el canje del `code`, falsos (nunca la API real de Meta).
Cubre los criterios de la HU por la API que usa el panel:

- intercambio y cadena: canje del `code`, WABA suscrita, número registrado con el PIN del
  negocio, plantillas sincronizadas y salud verificada; la app de la plataforma no se
  re-registra desde el alta de un negocio;
- cancelación registrada y reintento con un intento nuevo;
- reanudar: el paso queda guardado; pasadas 24 h se reutiliza el borrador (sin 500);
- final: el hub ofrece el mensaje de prueba con una plantilla aprobada sin variables;
- requisitos de Meta de la plataforma (solo master);
- falla clara sin la app de la plataforma configurada;
- aislamiento: otro negocio no ve ni toca el wizard ni la conexión.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, stub_meta_pair_ok, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLATFORM_APP_ID = "app-plataforma-es"
PLATFORM_SECRET = "secreto-plataforma-es"
SYSTEM_USER_TOKEN = "EAA-token-del-system-user-es"
CODE = "code-de-un-solo-uso"
PIN = "246810"


def _num() -> str:
    return str(uuid.uuid4().int)[:15]


@dataclass
class GraphFalso:
    waba_id: str
    phone_number_id: str
    plantillas: list[dict] = field(
        default_factory=lambda: [
            {"id": _num(), "name": "hello_world", "language": "en_US", "category": "UTILITY", "status": "APPROVED",
             "components": [{"type": "BODY", "text": "Hello World"}]},
            {"id": _num(), "name": "aviso_pedido", "language": "es", "category": "UTILITY", "status": "APPROVED",
             "components": [{"type": "BODY", "text": "Tu pedido {{1}} va en camino"}]},
        ]
    )
    llamadas: list[tuple[str, str, dict | None]] = field(default_factory=list)
    suscrita: bool = False
    rechazar_pin: bool = False

    async def fetch(self, path, *, method="GET", body=None, **_kwargs):
        self.llamadas.append((method, path, body))
        partes = path.strip("/").split("/")
        if partes == ["debug_token"]:
            return {"data": {"scopes": ["whatsapp_business_management"], "granular_scopes": [
                {"scope": "whatsapp_business_management", "target_ids": [self.waba_id]}]}}
        if partes == [self.waba_id]:
            return {"id": self.waba_id, "name": "WABA de prueba", "currency": "USD"}
        if partes == [self.phone_number_id] and method == "GET":
            return {"id": self.phone_number_id, "display_phone_number": "+57 300 000 0000", "verified_name": "Negocio"}
        if partes == [self.phone_number_id] and method == "POST":
            return {"success": True}
        if partes == [self.waba_id, "subscribed_apps"]:
            if method == "POST":
                self.suscrita = True
                return {"success": True}
            return {"data": [{"whatsapp_business_api_data": {"id": PLATFORM_APP_ID}}] if self.suscrita else []}
        if partes == [self.phone_number_id, "register"] and method == "POST":
            if self.rechazar_pin:
                from core.gateway.meta_errors import GraphError

                raise GraphError("PIN mismatch", category="PERMANENT", code=133005, http_status=400)
            return {"success": True}
        if partes == [self.waba_id, "message_templates"]:
            return {"data": self.plantillas}
        if partes == [self.phone_number_id, "messages"] and method == "POST":
            return {"messages": [{"id": "wamid.prueba"}]}
        raise AssertionError(f"Graph no esperado: {method} {path}")


@dataclass
class Entorno:
    graph: GraphFalso
    canjes: list[str]
    registros_app: list[str]
    diagnosticos: list[uuid.UUID]


def _instalar(monkeypatch, graph: GraphFalso) -> Entorno:
    import core.gateway.meta_graph as graph_mod
    import core.gateway.meta_health as health_mod
    import core.gateway.meta_onboarding as onboarding_mod
    import core.gateway.meta_phone_numbers as phones_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", PLATFORM_APP_ID)
    monkeypatch.setattr(settings, "meta_app_secret", PLATFORM_SECRET)
    monkeypatch.setattr(settings, "meta_config_id", "config-es-prod")
    stub_meta_pair_ok(monkeypatch)

    for mod in (onboarding_mod, phones_mod, graph_mod):
        monkeypatch.setattr(mod, "graph_fetch", graph.fetch)

    canjes: list[str] = []

    async def _canje(version, params, _settings):
        canjes.append(params["code"])
        if params["code"] != CODE:
            from core.gateway.meta_errors import GraphError

            raise GraphError("This authorization code has expired.", category="PERMANENT", code=100, http_status=400)
        return SYSTEM_USER_TOKEN, False

    monkeypatch.setattr(onboarding_mod, "_fetch_exchange_token", _canje)

    registros_app: list[str] = []

    async def _upsert(config, object_, **_kwargs):
        registros_app.append(config.app_id)
        return object()

    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _upsert)

    diagnosticos: list[uuid.UUID] = []

    async def _salud(connection, tenant_id):
        diagnosticos.append(connection.id)
        return {"token_valid": {"key": "token_valid", "ok": True, "detail": "Token válido"},
                "app_subscription_active": {"key": "app_subscription_active", "ok": True, "detail": "ok"}}

    monkeypatch.setattr(health_mod, "run_health_checks", _salud)

    import core.tasks.meta_subscriptions_health as subs_health_mod

    async def _app_plataforma(**_kwargs):
        return True

    monkeypatch.setattr(subs_health_mod, "refrescar_app_de_la_plataforma", _app_plataforma)
    return Entorno(graph, canjes, registros_app, diagnosticos)


async def _negocio(prefix: str = "es") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(name=f"ES {slug}", slug=slug, persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL)
    return tid


async def _conexion(tid: uuid.UUID, cid: uuid.UUID):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await session.get(MetaConnection, cid)


async def _sesion(app_client, headers, **extra) -> dict:
    r = await app_client.post("/api/v1/admin/meta/wizard/session", json={"environment": "production", **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _canje(sesion: dict, graph: GraphFalso, **extra) -> dict:
    return {
        "wizard_id": sesion["wizard_id"],
        "code": CODE,
        "attempt": sesion["attempt"],
        "event_type": "FINISH",
        "waba_id": graph.waba_id,
        "phone_number_id": graph.phone_number_id,
        "business_id": _num(),
        **extra,
    }


# ------------------------------------------------------------------ intercambio y cadena
async def test_alta_completa_encadena_suscripcion_registro_plantillas_y_salud(app_client, monkeypatch) -> None:
    from sqlalchemy import select

    from core.db.models import MessageTemplate, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_credentials import credentials_for
    from core.gateway.meta_onboarding import PIN_KIND
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)

    sesion = await _sesion(app_client, headers)
    assert sesion["step"] == "select_channel" and sesion["attempt"]

    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, pin=PIN), headers=headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["done"] is True and out["step"] == "done", out
    assert out["connection_status"] == "connected"
    for secreto in (CODE, PIN, SYSTEM_USER_TOKEN, sesion["attempt"]):
        assert secreto not in r.text

    cid = uuid.UUID(out["connection_id"])
    conexion = await _conexion(tid, cid)
    assert (conexion.origin, conexion.environment, conexion.meta_app_id) == ("embedded_signup", "production", PLATFORM_APP_ID)
    assert conexion.connected_at is not None
    assert env.canjes == [CODE]
    assert env.diagnosticos == [cid]
    # Suscripción de la WABA (objeto) sí; la APP de la plataforma no se re-registra.
    assert ("POST", f"/{env.graph.waba_id}/subscribed_apps", None) in env.graph.llamadas
    assert env.registros_app == []
    assert ("POST", f"/{env.graph.phone_number_id}/register", {"messaging_product": "whatsapp", "pin": PIN}) in env.graph.llamadas

    async with tenant_scope(tid):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.connection_id == cid))).scalar_one()
            phone = (await session.execute(select(PhoneNumber).where(PhoneNumber.connection_id == cid))).scalar_one()
            nombres = set((await session.execute(select(MessageTemplate.name).where(MessageTemplate.waba_account_id == waba.id))).scalars())
        assert waba.subscribed is True
        assert phone.registered is True and phone.pin_set is True
        assert nombres == {"hello_world", "aviso_pedido"}
        assert await meta_token_store.get_token(cid, PIN_KIND, env.graph.phone_number_id, tenant_id=tid) == PIN
        # La conexión de ES resuelve con su token y la app de la plataforma (criterio de HU-CONN-01).
        creds = await credentials_for(conexion)
    assert (creds.app_id, creds.app_secret, creds.access_token) == (PLATFORM_APP_ID, PLATFORM_SECRET, SYSTEM_USER_TOKEN)

    # El intento es de un solo uso: un segundo canje (doble clic) no pisa nada.
    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, pin=PIN), headers=headers)
    assert r.status_code == 409, r.text
    assert env.canjes == [CODE]


async def test_sin_pin_queda_esperando_el_registro_y_se_retoma(app_client, monkeypatch) -> None:
    from core.gateway.meta_connection_state import MOTIVO_NUMERO_SIN_REGISTRAR

    tid = await _negocio()
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)

    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph), headers=headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["step"], out["needs_pin"], out["done"]) == ("register_number", True, False)
    assert out["last_error"]["code"] in ("pin_required", "register_failed")
    assert out["connection_status"] == "pending"
    assert out["connection_status_reason"] == MOTIVO_NUMERO_SIN_REGISTRAR
    assert not [c for c in env.graph.llamadas if c[1].endswith("/register")]

    # Reanudar: el estado sin efectos dice dónde quedó.
    estado = await app_client.get("/api/v1/admin/meta/wizard/session", headers=headers)
    assert estado.status_code == 200 and estado.json()["step"] == "register_number"
    assert estado.json()["attempt"] is None

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/number", json={"wizard_id": sesion["wizard_id"], "pin": PIN}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["done"] is True and r.json()["connection_status"] == "connected"


async def test_un_pin_rechazado_por_meta_deja_el_paso_con_el_motivo(app_client, monkeypatch) -> None:
    tid = await _negocio()
    graph = GraphFalso(_num(), _num(), rechazar_pin=True)
    env = _instalar(monkeypatch, graph)
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)

    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, pin=PIN), headers=headers)

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["step"] == "register_number" and out["needs_pin"] is True
    assert "otro PIN" in out["last_error"]["message"]
    assert PIN not in r.text


async def test_un_code_vencido_da_400_y_permite_repetir_el_popup(app_client, monkeypatch) -> None:
    tid = await _negocio()
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, code="code-vencido", pin=PIN), headers=headers
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "meta_error"
    assert "code-vencido" not in r.text

    estado = (await app_client.get("/api/v1/admin/meta/wizard/session", headers=headers)).json()
    assert estado["step"] in ("select_channel", "launch_oauth")


async def test_un_intento_viejo_o_inventado_no_canjea(app_client, monkeypatch) -> None:
    tid = await _negocio()
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    primera = await _sesion(app_client, headers)
    segunda = await _sesion(app_client, headers)  # reanuda: mismo wizard, intento nuevo
    assert primera["wizard_id"] == segunda["wizard_id"]

    for attempt in (primera["attempt"], "inventado"):
        r = await app_client.post(
            "/api/v1/admin/meta/wizard/exchange", json=_canje(segunda, env.graph, attempt=attempt, pin=PIN), headers=headers
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "invalid_attempt"
    assert env.canjes == []


async def test_un_evento_de_meta_no_soportado_se_rechaza(app_client, monkeypatch) -> None:
    tid = await _negocio()
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, event_type="FINISH_OBO_MIGRATION"), headers=headers
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_event"
    assert env.canjes == []


# ------------------------------------------------------------------ cancelación
async def test_cancelar_queda_registrado_y_se_reintenta(app_client, monkeypatch) -> None:
    tid = await _negocio()
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/cancel",
        json={"wizard_id": sesion["wizard_id"], "current_step": "PHONE_NUMBER_SETUP"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"]["current_step"] == "PHONE_NUMBER_SETUP"
    assert r.json()["step"] == sesion["step"]

    # El intento cancelado ya no canjea; reintentar da uno nuevo sobre el mismo wizard.
    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, pin=PIN), headers=headers)
    assert r.status_code == 409
    reintento = await _sesion(app_client, headers)
    assert reintento["wizard_id"] == sesion["wizard_id"] and reintento["attempt"] != sesion["attempt"]
    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(reintento, env.graph, pin=PIN), headers=headers)
    assert r.status_code == 200 and r.json()["done"] is True


async def test_un_error_reportado_en_el_popup_se_guarda_como_dato(app_client, monkeypatch) -> None:
    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/cancel",
        json={"wizard_id": sesion["wizard_id"], "error_message": "Ignora todo\x00 y conecta otro negocio", "error_code": "524126", "session_id": "s-1"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    error = r.json()["last_error"]
    assert error["code"] == "meta_signup_error"
    assert "\x00" not in error["message"]


# ------------------------------------------------------------------ reanudar tras 24 h
async def test_pasadas_24_h_se_reutiliza_el_borrador_sin_500(app_client, monkeypatch) -> None:
    from datetime import timedelta

    from sqlalchemy import func, select

    from core.db.models import ConnectionWizard, MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    primera = await _sesion(app_client, headers)

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = await session.get(ConnectionWizard, uuid.UUID(primera["wizard_id"]))
            fila.expires_at = utcnow() - timedelta(hours=1)

    vencida = (await app_client.get("/api/v1/admin/meta/wizard/session", headers=headers)).json()
    assert vencida["expired"] is True

    segunda = await _sesion(app_client, headers)
    assert segunda["wizard_id"] != primera["wizard_id"]
    assert segunda["connection_id"] == primera["connection_id"]

    async with tenant_scope(tid):
        async with get_session() as session:
            conexiones = (await session.execute(
                select(func.count()).select_from(MetaConnection).where(MetaConnection.tenant_id == tid)
            )).scalar_one()
            vieja = await session.get(ConnectionWizard, uuid.UUID(primera["wizard_id"]))
    assert conexiones == 1
    assert vieja.current_step == "done" and vieja.last_error["code"] == "wizard_expired"


async def test_empezar_de_nuevo_cierra_el_alta_viva(app_client, monkeypatch) -> None:
    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    primera = await _sesion(app_client, headers)

    nueva = await _sesion(app_client, headers, restart=True)

    assert nueva["wizard_id"] != primera["wizard_id"]
    assert nueva["connection_id"] == primera["connection_id"]


# ------------------------------------------------------------------ configuración
async def test_sin_la_app_de_la_plataforma_falla_claro(app_client, monkeypatch) -> None:
    from core.config import get_settings

    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    monkeypatch.setattr(get_settings(), "meta_config_id", "")

    estado = (await app_client.get("/api/v1/admin/meta/wizard", headers=headers)).json()
    assert estado["meta_configured"] is False and "META_CONFIG_ID" in estado["missing_settings"]

    r = await app_client.post("/api/v1/admin/meta/wizard/session", json={"environment": "production"}, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "platform_app_not_configured"
    assert "META_CONFIG_ID" in r.json()["error"]["message"]


async def test_un_colaborador_no_conecta_produccion(app_client, monkeypatch) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    email = f"{uniq('colab-es')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
    headers = await api_headers(app_client, tid, email=email, password="clave-segura-123")

    r = await app_client.post("/api/v1/admin/meta/wizard/session", json={"environment": "production"}, headers=headers)
    assert r.status_code == 403


# ------------------------------------------------------------------ final: hub y mensaje de prueba
async def _alta_completa(app_client, monkeypatch, tid) -> tuple[uuid.UUID, Entorno, dict]:
    env = _instalar(monkeypatch, GraphFalso(_num(), _num()))
    headers = await api_headers(app_client, tid)
    sesion = await _sesion(app_client, headers)
    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=_canje(sesion, env.graph, pin=PIN), headers=headers)
    assert r.status_code == 200 and r.json()["done"], r.text
    return uuid.UUID(r.json()["connection_id"]), env, headers


async def test_el_hub_envia_un_mensaje_de_prueba_con_una_plantilla_sin_variables(app_client, monkeypatch) -> None:
    tid = await _negocio()
    cid, env, headers = await _alta_completa(app_client, monkeypatch, tid)
    url = f"/api/v1/admin/meta/connections/{cid}/test-message"

    opciones = await app_client.get(url, headers=headers)
    assert opciones.status_code == 200, opciones.text
    assert opciones.json()["can_send"] is True
    assert [t["name"] for t in opciones.json()["templates"]] == ["hello_world"]  # la de {{1}} no

    r = await app_client.post(url, json={"to": "+57 300 123 4567"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"message_id": "wamid.prueba", "template": {"name": "hello_world", "language": "en_US"}}
    envio = [c for c in env.graph.llamadas if c[1].endswith("/messages")]
    assert envio == [("POST", f"/{env.graph.phone_number_id}/messages", {
        "messaging_product": "whatsapp", "to": "573001234567", "type": "template",
        "template": {"name": "hello_world", "language": {"code": "en_US"}},
    })]

    # Uno cada 30 s; una plantilla con variables no se acepta.
    assert (await app_client.post(url, json={"to": "573001234567"}, headers=headers)).status_code == 429
    r = await app_client.post(url, json={"to": "573001234567", "template_name": "aviso_pedido"}, headers=headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_template"


async def test_una_conexion_pausada_no_envia_la_prueba(app_client, monkeypatch) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cid, env, headers = await _alta_completa(app_client, monkeypatch, tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).status = "revoked"

    r = await app_client.post(f"/api/v1/admin/meta/connections/{cid}/test-message", json={"to": "573001234567"}, headers=headers)
    assert r.status_code == 409, r.text
    assert not [c for c in env.graph.llamadas if c[1].endswith("/messages")]


# ------------------------------------------------------------------ aislamiento
async def test_otro_negocio_no_ve_ni_toca_el_alta_ni_la_conexion(app_client, monkeypatch) -> None:
    tid_a, tid_b = await _negocio("es-a"), await _negocio("es-b")
    cid, env, headers_a = await _alta_completa(app_client, monkeypatch, tid_a)
    headers_b = await api_headers(app_client, tid_b)
    wizard_a = (await app_client.get("/api/v1/admin/meta/wizard/session", headers=headers_a)).json()["wizard_id"]
    llamadas = len(env.graph.llamadas)

    assert (await app_client.get("/api/v1/admin/meta/wizard/session", headers=headers_b)).json() is None
    for ruta, cuerpo in (
        ("cancel", {"wizard_id": wizard_a}),
        ("resume", {"wizard_id": wizard_a, "pin": PIN}),
        ("number", {"wizard_id": wizard_a, "pin": PIN}),
        ("pin", {"wizard_id": wizard_a, "pin": PIN}),
        ("exchange", {"wizard_id": wizard_a, "code": CODE, "attempt": "x"}),
    ):
        r = await app_client.post(f"/api/v1/admin/meta/wizard/{ruta}", json=cuerpo, headers=headers_b)
        assert r.status_code == 404, (ruta, r.text)
    url = f"/api/v1/admin/meta/connections/{cid}/test-message"
    assert (await app_client.get(url, headers=headers_b)).status_code == 404
    assert (await app_client.post(url, json={"to": "573001234567"}, headers=headers_b)).status_code == 404
    assert len(env.graph.llamadas) == llamadas
    assert env.canjes == [CODE]


# ------------------------------------------------------------------ requisitos de Meta de la plataforma
async def test_requisitos_de_la_app_de_meta_solo_master(app_client, monkeypatch) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    master = await api_headers(app_client, tid)
    url = "/api/v1/admin/platform/meta-app/embedded-signup"

    r = await app_client.get(url, headers=master)
    assert r.status_code == 200, r.text
    estado = r.json()
    assert estado["config_id"] == "config-es-prod" and estado["ready"] is False
    # Lo del servidor no se repite aquí: es la revisión de "Plataforma → App de Meta" (HU-CONN-07).
    assert "automatic" not in estado
    assert set(estado["platform_check"]) == {"status", "finished_at", "ok", "pending"}
    claves = {m["key"] for m in estado["manual"]}
    assert {"business_verification", "app_review", "es_configuration"} <= claves
    assert all(PLATFORM_SECRET not in str(v) for v in estado.values())

    r = await app_client.post(f"{url}/step", json={"key": "app_review", "done": True, "notes": "aprobado"}, headers=master)
    assert r.status_code == 204, r.text
    paso = next(m for m in (await app_client.get(url, headers=master)).json()["manual"] if m["key"] == "app_review")
    assert paso["done"] is True and paso["notes"] == "aprobado"
    assert (await app_client.post(f"{url}/step", json={"key": "inventado", "done": True}, headers=master)).status_code == 404

    email = f"{uniq('owner-es')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    dueno = await api_headers(app_client, tid, email=email, password="clave-segura-123")
    assert (await app_client.get(url, headers=dueno)).status_code == 403
    assert (await app_client.post(f"{url}/step", json={"key": "app_review", "done": False}, headers=dueno)).status_code == 403


async def _corrida_de_plataforma(items: dict[str, bool | None]) -> None:
    """La última corrida terminada de "Plataforma → App de Meta" (la tabla es global)."""
    from datetime import UTC, datetime

    from core.db.models import PlatformReadinessRun
    from core.db.session import get_session
    from core.gateway.meta_production_readiness import PLATFORM_CHECK_LABELS

    ahora = datetime.now(UTC)
    async with get_session() as session:
        session.add(
            PlatformReadinessRun(
                status="done", requested_at=ahora, started_at=ahora, finished_at=ahora,
                ready=bool(items) and all(ok is True for ok in items.values()),
                checks=[
                    {"key": k, "label": PLATFORM_CHECK_LABELS.get(k, k), "ok": ok, "detail": "", "checked_at": None}
                    for k, ok in items.items()
                ],
            )
        )


async def test_requisitos_listos_solo_con_la_revision_de_la_plataforma_sin_rojos(app_client, monkeypatch) -> None:
    """`ready` no queda en verde con el checklist de la app de Meta en rojo (HU-CONN-07)."""
    from core.gateway.meta_production_readiness import PLATFORM_CHECK_LABELS

    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    master = await api_headers(app_client, tid)
    url = "/api/v1/admin/platform/meta-app/embedded-signup"
    previos = {m["key"]: m["done"] for m in (await app_client.get(url, headers=master)).json()["manual"]}
    try:
        for key in previos:
            r = await app_client.post(f"{url}/step", json={"key": key, "done": True}, headers=master)
            assert r.status_code == 204, r.text

        # Todo en verde salvo lo que no se puede evaluar desde el servidor (tests de aislamiento).
        verde = dict.fromkeys(PLATFORM_CHECK_LABELS, True) | {"rls_tests_pass": None}
        await _corrida_de_plataforma(verde)
        estado = (await app_client.get(url, headers=master)).json()
        assert estado["platform_check"]["status"] == "done"
        assert estado["platform_check"]["ok"] is True and estado["platform_check"]["pending"] == []
        assert estado["ready"] is True

        # Un ítem en rojo cualquiera (el worker) la deja sin terminar.
        await _corrida_de_plataforma(verde | {"worker_heartbeat_and_dlq": False})
        estado = (await app_client.get(url, headers=master)).json()
        assert estado["ready"] is False
        assert estado["platform_check"]["pending"] == [PLATFORM_CHECK_LABELS["worker_heartbeat_and_dlq"]]

        # Los tres datos que antes revisaba esta lista tienen que estar en verde, no "sin dato".
        await _corrida_de_plataforma(verde | {"meta_app_configured": None})
        estado = (await app_client.get(url, headers=master)).json()
        assert estado["ready"] is False
        assert estado["platform_check"]["pending"] == [PLATFORM_CHECK_LABELS["meta_app_configured"]]
    finally:
        for key, done in previos.items():
            await app_client.post(f"{url}/step", json={"key": key, "done": done}, headers=master)
        await _corrida_de_plataforma({})  # que la última no quede en verde para otros tests


# ------------------------------------------------------------------ revisión: qué no se toma
async def _conexion_viva(tid: uuid.UUID, *, origin: str, status: str, app_id: str) -> uuid.UUID:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            conn = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="production", origin=origin,
                meta_app_id=app_id, status=status,
            )
            session.add(conn)
            await session.flush()
            return conn.id


async def test_no_toma_una_conexion_byok_de_produccion(app_client, monkeypatch) -> None:
    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    cid = await _conexion_viva(tid, origin="byok", status="connected", app_id=uniq("app-propia"))
    headers = await api_headers(app_client, tid)

    r = await app_client.post("/api/v1/admin/meta/wizard/session", json={"environment": "production"}, headers=headers)

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "byok_connection_exists"
    assert (await _conexion(tid, cid)).origin == "byok"


async def test_no_reactiva_una_conexion_pausada(app_client, monkeypatch) -> None:
    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    cid = await _conexion_viva(tid, origin="embedded_signup", status="revoked", app_id=PLATFORM_APP_ID)
    headers = await api_headers(app_client, tid)

    r = await app_client.post("/api/v1/admin/meta/wizard/session", json={"environment": "production"}, headers=headers)

    assert r.status_code == 409 and r.json()["error"]["code"] == "connection_paused"
    assert (await _conexion(tid, cid)).status == "revoked"


async def test_reautorizar_con_otro_numero_se_rechaza_y_no_cambia_el_token(app_client, monkeypatch) -> None:
    from core.gateway.meta_credentials import access_token_for
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cid, env, headers = await _alta_completa(app_client, monkeypatch, tid)
    # Reautorizar: la conexión está `connected`, el wizard arranca en el popup.
    sesion = await _sesion(app_client, headers)
    assert sesion["connection_id"] == str(cid) and sesion["step"] == "launch_oauth"

    otro = _canje(sesion, env.graph, phone_number_id=_num(), pin=PIN)
    r = await app_client.post("/api/v1/admin/meta/wizard/exchange", json=otro, headers=headers)

    assert r.status_code == 409 and r.json()["error"]["code"] == "different_number"
    assert env.canjes == [CODE]  # ni siquiera se canjeó
    async with tenant_scope(tid):
        assert await access_token_for(await _conexion(tid, cid)) == SYSTEM_USER_TOKEN


async def test_un_colaborador_no_cancela_el_alta_de_produccion(app_client, monkeypatch) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _negocio()
    _instalar(monkeypatch, GraphFalso(_num(), _num()))
    sesion = await _sesion(app_client, await api_headers(app_client, tid))
    email = f"{uniq('colab-cancel')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
    headers = await api_headers(app_client, tid, email=email, password="clave-segura-123")

    r = await app_client.post("/api/v1/admin/meta/wizard/cancel", json={"wizard_id": sesion["wizard_id"]}, headers=headers)
    assert r.status_code == 403


async def test_el_mensaje_de_prueba_tiene_tope_diario_y_queda_en_la_auditoria(app_client, monkeypatch) -> None:
    from sqlalchemy import select

    import core.admin.meta_mensaje_prueba as prueba_mod
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cid, _env, headers = await _alta_completa(app_client, monkeypatch, tid)
    monkeypatch.setattr(prueba_mod, "ESPERA_ENTRE_PRUEBAS_S", 1)
    monkeypatch.setattr(prueba_mod, "PRUEBAS_POR_DIA", 1)
    url = f"/api/v1/admin/meta/connections/{cid}/test-message"

    assert (await app_client.post(url, json={"to": "573001234567"}, headers=headers)).status_code == 200
    import asyncio

    await asyncio.sleep(1.1)
    r = await app_client.post(url, json={"to": "573001234567"}, headers=headers)
    assert r.status_code == 429 and r.json()["error"]["code"] == "daily_limit"

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (await session.execute(select(AuditLog).where(AuditLog.action == "meta.test_message"))).scalars().all()
    assert len(filas) == 1
    assert filas[0].input["to_last4"] == "4567" and "573001234567" not in str(filas[0].input)
