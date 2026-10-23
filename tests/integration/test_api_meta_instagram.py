"""API del alta de Instagram (`/api/v1/admin/meta/instagram*`, ADR-0152, HU-CHAN-04).

El flujo lo cubre `test_instagram_alta.py` contra el servicio; aquí, lo propio de la API: permisos, contrato de
las respuestas, errores con código estable y que ningún token salga en una respuesta.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import (
    ADMIN_EMAIL,
    APP_ID_PLATAFORMA,
    MetaSimulado,
    api_headers,
    pagina_con_instagram,
    uniq,
)
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
        name=f"Tenant {slug}", slug=slug, business_description="Test de la API de Instagram.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL, whatsapp=None,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text("update tenant_subscriptions set plan_id = :p where tenant_id = :t"), {"p": plan, "t": tid}
            )
    return tid


@pytest.fixture
def meta(monkeypatch):
    import core.gateway.meta_instagram_alta as alta
    import core.gateway.meta_onboarding as onboarding
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", APP_ID_PLATAFORMA)
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_config_id", "config-whatsapp")
    monkeypatch.setattr(settings, "meta_messenger_config_id", "config-messenger")
    monkeypatch.setattr(settings, "meta_instagram_config_id", "config-instagram")

    async def canje(version, params, settings_, *, redirect_path="/x"):
        return "token-del-usuario", False

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", canje)

    def instalar(paginas, **kw) -> MetaSimulado:
        simulado = MetaSimulado(paginas, **kw)
        monkeypatch.setattr(alta, "graph_fetch", simulado.graph_fetch)
        return simulado

    return instalar


async def _intento(app_client, headers) -> str:
    r = await app_client.post(f"{BASE}/attempt", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["attempt"]


def _ids() -> tuple[str, str]:
    n = uuid.uuid4().int
    return str(n % 10**12), str(n // 10**12 % 10**15)


# ------------------------------------------------------------------ estado
async def test_el_estado_dice_si_esta_lista_la_app_y_si_el_plan_lo_incluye(app_client, meta) -> None:
    basic, free = await _new_tenant("api-ig-basic"), await _new_tenant("api-ig-free", plan="free")
    meta([])

    r = await app_client.get(BASE, headers=await api_headers(app_client, basic))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "meta_configured": True, "app_id": APP_ID_PLATAFORMA,
        # La de Instagram es propia: no es la de Messenger ni la de WhatsApp.
        "config_id": "config-instagram", "graph_api_version": r.json()["graph_api_version"],
        "missing_settings": [], "plan_includes": True,
        # Instagram Login (ruta b) está apagada por defecto: dice qué falta para ofrecerla.
        "login_available": False, "login_missing": ["INSTAGRAM_LOGIN_ENABLED"],
    }
    r = await app_client.get(BASE, headers=await api_headers(app_client, free))
    assert r.json()["plan_includes"] is False


async def test_sin_su_configuracion_propia_dice_que_falta_y_no_usa_la_de_messenger(app_client, meta, monkeypatch) -> None:
    from core.config import get_settings

    tid = await _new_tenant("api-ig-config")
    meta([])
    monkeypatch.setattr(get_settings(), "meta_instagram_config_id", "")
    headers = await api_headers(app_client, tid)

    estado = (await app_client.get(BASE, headers=headers)).json()

    assert (estado["meta_configured"], estado["missing_settings"], estado["config_id"]) == (
        False, ["META_INSTAGRAM_CONFIG_ID"], "",
    )
    assert "platform-secret-test" not in str(estado)  # el App Secret nunca sale


# ------------------------------------------------------------------ permisos
async def test_sin_sesion_es_401(app_client) -> None:
    for metodo, ruta in (("get", ""), ("post", "/attempt"), ("post", "/discover"), ("post", "/connect")):
        r = await getattr(app_client, metodo)(f"{BASE}{ruta}")
        assert r.status_code == 401, (metodo, ruta, r.status_code)


async def test_un_colaborador_de_solo_lectura_no_puede_conectar(app_client, meta) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid = await _new_tenant("api-ig-colab")
    meta([])
    email = f"{uniq('colab')}@correo-prueba.co"
    async with get_session() as s:
        admin = Admin(email=email, password_hash=hash_password(CLAVE), role="colaborador")
        s.add(admin)
        await s.flush()
        s.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
    headers = await api_headers(app_client, tid, email=email, password=CLAVE)

    for metodo, ruta, cuerpo in (
        ("get", "", None),
        ("post", "/attempt", None),
        ("post", "/discover", {"code": "c", "attempt": "a"}),
        ("post", "/connect", {"connection_id": str(uuid.uuid4()), "ig_user_ids": ["123"]}),
    ):
        r = await getattr(app_client, metodo)(f"{BASE}{ruta}", headers=headers, **({"json": cuerpo} if cuerpo else {}))
        assert r.status_code == 403, (metodo, ruta, r.status_code)


# ------------------------------------------------------------------ el flujo por HTTP
async def test_el_flujo_por_http_conecta_la_cuenta_y_no_expone_tokens(app_client, meta) -> None:
    tid = await _new_tenant("api-ig-flujo")
    page, ig = _ids()
    otra_page, otra_ig = _ids()
    simulado = meta([
        pagina_con_instagram(page, ig, username="mitienda"),
        pagina_con_instagram(otra_page, otra_ig, tasks=("ANALYZE",)),
        {"id": "999", "name": "Sin IG", "tasks": ["MANAGE"]},
    ])
    headers = await api_headers(app_client, tid)

    intento = await _intento(app_client, headers)
    r = await app_client.post(f"{BASE}/discover", headers=headers, json={"code": "codigo-del-popup", "attempt": intento})
    assert r.status_code == 200, r.text
    descubrimiento = r.json()
    assert {c["ig_user_id"]: c["eligible"] for c in descubrimiento["accounts"]} == {ig: True, otra_ig: False}
    assert descubrimiento["pages_without_instagram"] == 1
    cuenta = next(c for c in descubrimiento["accounts"] if c["ig_user_id"] == ig)
    assert (cuenta["username"], cuenta["page_id"], cuenta["already_connected"]) == ("mitienda", page, False)
    assert "tok-" not in r.text and "token-del-usuario" not in r.text

    r = await app_client.post(
        f"{BASE}/connect", headers=headers,
        json={"connection_id": descubrimiento["connection_id"], "ig_user_ids": [ig]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == []
    (conectada,) = r.json()["connected"]
    assert (conectada["ig_user_id"], conectada["subscribed"]) == (ig, True)
    assert "messages" in conectada["subscribed_fields"]
    assert "tok-" not in r.text
    assert any(t == f"tok-{page}" for _m, _p, t in simulado.llamadas)


# ------------------------------------------------------------------ errores con código estable
async def test_un_intento_repetido_o_inventado_es_400_invalid_attempt(app_client, meta) -> None:
    tid = await _new_tenant("api-ig-intento")
    meta([pagina_con_instagram(*_ids())])
    headers = await api_headers(app_client, tid)
    intento = await _intento(app_client, headers)
    cuerpo = {"code": "codigo-del-popup", "attempt": intento}

    assert (await app_client.post(f"{BASE}/discover", headers=headers, json=cuerpo)).status_code == 200
    r = await app_client.post(f"{BASE}/discover", headers=headers, json=cuerpo)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_attempt")


async def test_un_plan_sin_instagram_es_409_con_su_codigo(app_client, meta) -> None:
    tid = await _new_tenant("api-ig-plan", plan="free")
    meta([])
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/attempt", headers=headers)

    assert (r.status_code, r.json()["error"]["code"]) == (409, "plan_channel_not_included")
    assert "no incluye Instagram" in r.json()["error"]["message"]


async def test_un_id_de_cuenta_con_formato_raro_es_400(app_client, meta) -> None:
    tid = await _new_tenant("api-ig-id")
    meta([])
    headers = await api_headers(app_client, tid)

    for malo in ("123/subscribed_apps", "12 34", "abc", "1" * 40, "1?fields=x"):
        r = await app_client.post(
            f"{BASE}/connect", headers=headers, json={"connection_id": str(uuid.uuid4()), "ig_user_ids": [malo]}
        )
        assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_id"), malo


async def test_sin_cuentas_o_con_demasiadas_es_un_error_de_validacion(app_client, meta) -> None:
    tid = await _new_tenant("api-ig-vacio")
    meta([])
    headers = await api_headers(app_client, tid)
    conexion = str(uuid.uuid4())

    vacia = await app_client.post(f"{BASE}/connect", headers=headers, json={"connection_id": conexion, "ig_user_ids": []})
    muchas = await app_client.post(
        f"{BASE}/connect", headers=headers, json={"connection_id": conexion, "ig_user_ids": [str(i) for i in range(51)]}
    )
    assert vacia.status_code == 422 and muchas.status_code == 422


async def test_una_conexion_que_no_existe_es_404(app_client, meta) -> None:
    tid = await _new_tenant("api-ig-404")
    meta([])
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/connect", headers=headers, json={"connection_id": str(uuid.uuid4()), "ig_user_ids": ["123"]})

    assert r.status_code == 404


async def test_la_conexion_de_messenger_no_sirve_para_conectar_cuentas_de_instagram(app_client, meta) -> None:
    """Las cuentas de Meta de cada canal son distintas: un `connection_id` de Messenger no abre el alta de Instagram."""
    tid = await _new_tenant("api-ig-cruce")
    meta([pagina_con_instagram(*_ids())])
    headers = await api_headers(app_client, tid)
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = MetaConnection(tenant_id=tid, channel="messenger", origin="manual", meta_app_id=uniq("app"), status="connected")
            session.add(cuenta)
            await session.flush()
            messenger_id = cuenta.id

    r = await app_client.post(f"{BASE}/connect", headers=headers, json={"connection_id": str(messenger_id), "ig_user_ids": ["123"]})

    assert r.status_code == 404
