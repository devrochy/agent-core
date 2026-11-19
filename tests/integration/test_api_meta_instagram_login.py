"""API de Instagram Login, cuentas y diagnóstico (`/api/v1/admin/meta/instagram/*`, HU-CHAN-04, ADR-0152).

El flujo lo cubre `test_instagram_login.py` contra el servicio; aquí, lo propio de la API: permisos, contrato, que ningún
token salga en una respuesta, y la prueba de humo (qué cuenta y qué eventos puede ver cada negocio).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/meta/instagram"
CLAVE = "Clave-de-prueba-1"


async def _new_tenant(prefix: str, *, plan: str = "basic") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de la API de Instagram Login.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL, whatsapp=None,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text("update tenant_subscriptions set plan_id = :p where tenant_id = :t"), {"p": plan, "t": tid}
            )
    return tid


@pytest.fixture
def ig_login(monkeypatch):
    """Instagram Login encendido y configurado, con Meta simulado: `autoriza(ig)` da un código que canjea esa cuenta."""
    import core.gateway.meta_instagram_login_alta as alta
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "instagram_app_id", "ig-app-test")
    monkeypatch.setattr(settings, "instagram_app_secret", "ig-secreto-test")
    monkeypatch.setattr(settings, "instagram_login_redirect_uri", "https://panel.test/conexiones/instagram/callback")
    monkeypatch.setattr(settings, "instagram_login_enabled", True)
    cuentas: dict[str, str] = {}

    async def canjear_codigo(code):
        return f"corto-{code}", cuentas[code]

    async def token_largo(corto):
        return f"largo-{corto}", datetime.now(UTC) + timedelta(days=60)

    async def datos(token):
        return cuentas[token.removeprefix("largo-corto-")], "mitienda"

    async def suscribir(ig_user_id, token, version):
        return True, ("messages",)

    monkeypatch.setattr(alta, "canjear_codigo", canjear_codigo)
    monkeypatch.setattr(alta, "canjear_por_token_largo", token_largo)
    monkeypatch.setattr(alta, "datos_de_la_cuenta", datos)
    monkeypatch.setattr(alta, "suscribir_y_verificar", suscribir)

    def autoriza(ig_user_id: str) -> str:
        code = uuid.uuid4().hex[:12]
        cuentas[code] = ig_user_id
        return code

    return autoriza


def _ig() -> str:
    return str(uuid.uuid4().int % 10**17)


async def _conectar(app_client, headers, code: str):
    r = await app_client.post(f"{BASE}/login/start", headers=headers)
    assert r.status_code == 201, r.text
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    return await app_client.post(f"{BASE}/login/complete", headers=headers, json={"code": code, "state": state})


# ------------------------------------------------------------------ estado y permisos
async def test_el_estado_dice_si_instagram_login_esta_disponible_y_que_falta(app_client, ig_login, monkeypatch) -> None:
    from core.config import get_settings

    tid = await _new_tenant("apil-estado")
    headers = await api_headers(app_client, tid)

    estado = (await app_client.get(BASE, headers=headers)).json()
    assert (estado["login_available"], estado["login_missing"]) == (True, [])

    monkeypatch.setattr(get_settings(), "instagram_login_enabled", False)
    estado = (await app_client.get(BASE, headers=headers)).json()
    assert (estado["login_available"], estado["login_missing"]) == (False, ["INSTAGRAM_LOGIN_ENABLED"])
    assert "ig-secreto-test" not in str(estado)  # el App Secret nunca sale


async def test_sin_sesion_es_401_y_un_colaborador_de_solo_lectura_no_conecta(app_client, ig_login) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    for metodo, ruta in (
        ("post", "/login/start"), ("post", "/login/complete"), ("get", "/accounts"),
        ("post", "/accounts/123/diagnostic"), ("get", "/accounts/123/diagnostic"),
    ):
        assert (await getattr(app_client, metodo)(f"{BASE}{ruta}")).status_code == 401, (metodo, ruta)

    tid = await _new_tenant("apil-colab")
    email = f"{uniq('colab')}@correo-prueba.co"
    async with get_session() as s:
        admin = Admin(email=email, password_hash=hash_password(CLAVE), role="colaborador")
        s.add(admin)
        await s.flush()
        s.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
    headers = await api_headers(app_client, tid, email=email, password=CLAVE)
    for ruta, cuerpo in (("/login/start", None), ("/login/complete", {"code": "c", "state": "s"})):
        r = await app_client.post(f"{BASE}{ruta}", headers=headers, **({"json": cuerpo} if cuerpo else {}))
        assert r.status_code == 403, ruta


# ------------------------------------------------------------------ el flujo por HTTP
async def test_el_flujo_por_http_conecta_la_cuenta_y_no_expone_tokens(app_client, ig_login) -> None:
    tid, ig = await _new_tenant("apil-flujo"), _ig()
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/login/start", headers=headers)
    assert r.status_code == 201, r.text
    url = urlparse(r.json()["url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "www.instagram.com" and q["client_id"] == "ig-app-test"
    assert "ig-secreto-test" not in r.text

    r = await _conectar(app_client, headers, ig_login(ig))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo["ig_user_id"], cuerpo["username"], cuerpo["subscribed"]) == (ig, "mitienda", True)
    assert "largo-" not in r.text and "corto-" not in r.text


async def test_un_state_repetido_o_inventado_es_400_y_apagado_es_409(app_client, ig_login, monkeypatch) -> None:
    from core.config import get_settings

    tid = await _new_tenant("apil-errores")
    headers = await api_headers(app_client, tid)
    start = await app_client.post(f"{BASE}/login/start", headers=headers)
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]
    cuerpo = {"code": ig_login(_ig()), "state": state}

    assert (await app_client.post(f"{BASE}/login/complete", headers=headers, json=cuerpo)).status_code == 200
    r = await app_client.post(f"{BASE}/login/complete", headers=headers, json=cuerpo)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_attempt")

    monkeypatch.setattr(get_settings(), "instagram_login_enabled", False)
    r = await app_client.post(f"{BASE}/login/start", headers=headers)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "instagram_login_unavailable")


async def test_el_code_y_el_state_no_se_devuelven_en_un_error_de_validacion(app_client, ig_login) -> None:
    tid = await _new_tenant("apil-secreto")
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/login/complete", headers=headers, json={"code": "secreto-del-codigo", "state": 123})

    assert r.status_code == 422 and "secreto-del-codigo" not in r.text


# ------------------------------------------------------------------ cuentas
async def test_las_cuentas_salen_con_su_estado_y_el_vencimiento_sin_tokens(app_client, ig_login) -> None:
    tid, ig = await _new_tenant("apil-cuentas"), _ig()
    headers = await api_headers(app_client, tid)
    assert (await app_client.get(f"{BASE}/accounts", headers=headers)).json() == {"accounts": []}
    await _conectar(app_client, headers, ig_login(ig))

    r = await app_client.get(f"{BASE}/accounts", headers=headers)

    (cuenta,) = r.json()["accounts"]
    assert (cuenta["ig_user_id"], cuenta["username"], cuenta["login_route"], cuenta["status"]) == (
        ig, "mitienda", "ig_instagram_login", "connected",
    )
    assert cuenta["subscribed_fields"] == ["messages"] and cuenta["page_id"] is None
    vence = datetime.fromisoformat(cuenta["token_expires_at"])
    assert timedelta(days=59) < vence - datetime.now(UTC) <= timedelta(days=60)
    assert "largo-" not in r.text


async def test_un_negocio_no_ve_las_cuentas_de_otro(app_client, ig_login) -> None:
    a, b, ig = await _new_tenant("apil-ver-a"), await _new_tenant("apil-ver-b"), _ig()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    await _conectar(app_client, ha, ig_login(ig))

    assert (await app_client.get(f"{BASE}/accounts", headers=hb)).json() == {"accounts": []}
    assert (await app_client.post(f"{BASE}/accounts/{ig}/diagnostic", headers=hb)).status_code == 404
    assert (await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": "2026-01-01T00:00:00Z"}, headers=hb)).status_code == 404


# ------------------------------------------------------------------ prueba de humo
async def _evento(tenant_id: uuid.UUID, ig: str, *, recibido: datetime, cliente: str = "IGSID9") -> None:
    """Un mensaje de un cliente a la cuenta `ig`, como lo deja el registro de entregas del webhook."""
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    async with get_session() as session:
        session.add(
            WebhookEvent(
                tenant_id=tenant_id, meta_app_id="app-test", object_type="instagram", field="messages",
                external_id=uniq("mid"), signature_ok=True, received_at=recibido,
                payload={"sender": {"id": cliente}, "recipient": {"id": ig}, "message": {"mid": uniq("m"), "text": "hola"}},
            )
        )


async def test_la_prueba_de_humo_detecta_el_mensaje_que_llega_despues_de_empezar(app_client, ig_login) -> None:
    tid, ig = await _new_tenant("apil-humo"), _ig()
    headers = await api_headers(app_client, tid)
    await _conectar(app_client, headers, ig_login(ig))
    await _evento(tid, ig, recibido=datetime.now(UTC) - timedelta(minutes=10))  # anterior: no cuenta

    inicio = await app_client.post(f"{BASE}/accounts/{ig}/diagnostic", headers=headers)
    assert inicio.status_code == 200 and inicio.json()["wait_seconds"] == 90
    since = inicio.json()["since"]

    sin_mensaje = await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": since}, headers=headers)
    assert sin_mensaje.json()["arrived"] is False

    await _evento(tid, ig, recibido=datetime.now(UTC))
    con_mensaje = await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": since}, headers=headers)
    assert con_mensaje.json() == {"arrived": True, "likely_cause": None}


async def test_los_eventos_de_otro_negocio_o_de_otra_cuenta_no_cuentan(app_client, ig_login) -> None:
    a, b, ig, otra = await _new_tenant("apil-aisla-a"), await _new_tenant("apil-aisla-b"), _ig(), _ig()
    ha = await api_headers(app_client, a)
    await _conectar(app_client, ha, ig_login(ig))
    since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await _evento(b, ig, recibido=datetime.now(UTC))        # mismo ig_user_id, pero de OTRO negocio
    await _evento(a, otra, recibido=datetime.now(UTC))      # del negocio, pero a OTRA cuenta

    r = await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": since}, headers=ha)

    assert r.json()["arrived"] is False


async def test_si_no_llega_nada_dice_la_causa_mas_probable(app_client, ig_login) -> None:
    from sqlalchemy import select

    from core.db.models import IgAccount
    from core.db.session import get_session
    from core.gateway.meta_instagram import con_estado
    from core.shared.tenant import tenant_scope

    tid, ig = await _new_tenant("apil-causa"), _ig()
    headers = await api_headers(app_client, tid)
    await _conectar(app_client, headers, ig_login(ig))
    since = datetime.now(UTC).isoformat()

    async def causa() -> str:
        r = await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": since}, headers=headers)
        return r.json()["likely_cause"]

    # Todo parece bien: casi siempre es «Permitir acceso a los mensajes» apagado, que Meta nunca reporta como error.
    assert await causa() == "access_to_messages_off"

    async def editar(**cambios):
        async with tenant_scope(tid):
            async with get_session() as session:
                fila = (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).scalar_one()
                fila.raw = {**con_estado(fila.raw, cambios.get("estado", "connected"), cambios.get("motivo")),
                            "subscribed_fields": cambios.get("campos", fila.raw.get("subscribed_fields", []))}

    await editar(campos=[])
    assert await causa() == "not_subscribed"
    await editar(campos=["messages"], estado="degraded", motivo="reconecta")
    assert await causa() == "token_degraded"


async def test_un_id_de_cuenta_con_formato_raro_es_400(app_client, ig_login) -> None:
    tid = await _new_tenant("apil-id")
    headers = await api_headers(app_client, tid)
    for malo in ("abc", "12%2034", "1" * 40):
        assert (await app_client.post(f"{BASE}/accounts/{malo}/diagnostic", headers=headers)).status_code in (400, 404), malo


async def test_el_diagnostico_ignora_lo_que_no_es_un_mensaje_y_acota_la_ventana(app_client, ig_login) -> None:
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    tid, ig = await _new_tenant("apil-solo-msj"), _ig()
    headers = await api_headers(app_client, tid)
    await _conectar(app_client, headers, ig_login(ig))
    since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    async with get_session() as session:  # una lectura (`read`) dirigida a la cuenta: no es un mensaje
        session.add(
            WebhookEvent(
                tenant_id=tid, meta_app_id="app-test", object_type="instagram", field="messaging", external_id=uniq("h"),
                signature_ok=True, received_at=datetime.now(UTC),
                payload={"sender": {"id": "IGSID9"}, "recipient": {"id": ig}, "read": {"mid": "m_1"}},
            )
        )
    r = await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": since}, headers=headers)
    assert r.json()["arrived"] is False

    # Un mensaje de hace 3 días queda fuera aunque se pida `since` de hace una semana.
    await _evento(tid, ig, recibido=datetime.now(UTC) - timedelta(days=3))
    antiguo = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    r = await app_client.get(f"{BASE}/accounts/{ig}/diagnostic", params={"since": antiguo}, headers=headers)
    assert r.json()["arrived"] is False


async def test_un_id_con_digitos_que_no_son_ascii_es_400(app_client, ig_login) -> None:
    tid = await _new_tenant("apil-unicode")
    headers = await api_headers(app_client, tid)
    r = await app_client.post(f"{BASE}/connect", headers=headers, json={"connection_id": str(uuid.uuid4()), "ig_user_ids": ["١٢٣"]})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_id")
