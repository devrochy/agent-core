"""API del alta de Messenger (`/api/v1/admin/meta/messenger*`, ADR-0152, HU-CHAN-01).

El flujo lo cubre `test_messenger_alta.py` contra el servicio; aquí, lo propio de la API: permisos, contrato
de las respuestas, errores con código estable y que ningún token salga en una respuesta.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, APP_ID_PLATAFORMA, MetaSimulado, api_headers, uniq
from _api_helpers import pagina_de_meta as _pagina
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/meta/messenger"
CLAVE = "Clave-de-prueba-1"


async def _new_tenant(prefix: str, *, plan: str = "basic") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de la API de Messenger.",
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
    import core.gateway.meta_messenger_alta as alta
    import core.gateway.meta_onboarding as onboarding
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", APP_ID_PLATAFORMA)
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_config_id", "config-whatsapp")
    monkeypatch.setattr(settings, "meta_messenger_config_id", "config-messenger")

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


# ------------------------------------------------------------------ estado
async def test_el_estado_dice_si_esta_lista_la_app_y_si_el_plan_lo_incluye(app_client, meta) -> None:
    basic, free = await _new_tenant("api-msgr-basic"), await _new_tenant("api-msgr-free", plan="free")
    meta([])

    r = await app_client.get(BASE, headers=await api_headers(app_client, basic))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "meta_configured": True, "app_id": APP_ID_PLATAFORMA,
        # La Login Configuration propia de Messenger gana sobre la de Embedded Signup de WhatsApp.
        "config_id": "config-messenger", "graph_api_version": r.json()["graph_api_version"],
        "missing_settings": [], "plan_includes": True,
    }

    r = await app_client.get(BASE, headers=await api_headers(app_client, free))
    assert r.json()["plan_includes"] is False


async def test_sin_configuracion_propia_usa_la_de_la_plataforma_y_sin_nada_dice_que_falta(
    app_client, meta, monkeypatch
) -> None:
    from core.config import get_settings

    tid = await _new_tenant("api-msgr-config")
    meta([])
    settings = get_settings()
    headers = await api_headers(app_client, tid)

    monkeypatch.setattr(settings, "meta_messenger_config_id", "")
    assert (await app_client.get(BASE, headers=headers)).json()["config_id"] == "config-whatsapp"

    monkeypatch.setattr(settings, "meta_config_id", "")
    estado = (await app_client.get(BASE, headers=headers)).json()
    assert (estado["meta_configured"], estado["missing_settings"]) == (False, ["META_MESSENGER_CONFIG_ID"])
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

    tid = await _new_tenant("api-msgr-colab")
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
        ("post", "/connect", {"connection_id": str(uuid.uuid4()), "page_ids": ["123"]}),
    ):
        r = await getattr(app_client, metodo)(f"{BASE}{ruta}", headers=headers, **({"json": cuerpo} if cuerpo else {}))
        assert r.status_code == 403, (metodo, ruta, r.status_code)


# ------------------------------------------------------------------ el flujo por HTTP
async def test_el_flujo_por_http_conecta_la_pagina_y_no_expone_tokens(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-flujo")
    page = str(int(uuid.uuid4().int % 10**12))  # los IDs de Página son numéricos
    simulado = meta([_pagina(page), _pagina(str(int(page) + 1), tasks=("ANALYZE",))])
    headers = await api_headers(app_client, tid)

    intento = await _intento(app_client, headers)
    r = await app_client.post(f"{BASE}/discover", headers=headers, json={"code": "codigo-del-popup", "attempt": intento})
    assert r.status_code == 200, r.text
    descubrimiento = r.json()
    assert {p["page_id"]: p["eligible"] for p in descubrimiento["pages"]} == {page: True, str(int(page) + 1): False}
    assert all(p["already_connected"] is False for p in descubrimiento["pages"])
    assert "tok-" not in r.text and "token-del-usuario" not in r.text

    r = await app_client.post(
        f"{BASE}/connect", headers=headers,
        json={"connection_id": descubrimiento["connection_id"], "page_ids": [page]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == []
    (conectada,) = r.json()["connected"]
    assert (conectada["page_id"], conectada["subscribed"]) == (page, True)
    assert conectada["subscribed_fields"] == ["messages", "messaging_postbacks"]
    assert "tok-" not in r.text
    assert any(t == f"tok-{page}" for _m, _p, t in simulado.llamadas)


# ------------------------------------------------------------------ errores con código estable
async def test_un_intento_repetido_o_inventado_es_400_invalid_attempt(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-intento")
    meta([_pagina("111")])
    headers = await api_headers(app_client, tid)
    intento = await _intento(app_client, headers)
    cuerpo = {"code": "codigo-del-popup", "attempt": intento}

    assert (await app_client.post(f"{BASE}/discover", headers=headers, json=cuerpo)).status_code == 200
    r = await app_client.post(f"{BASE}/discover", headers=headers, json=cuerpo)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_attempt")

    r = await app_client.post(f"{BASE}/discover", headers=headers, json={"code": "x", "attempt": "inventado"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_attempt")


async def test_un_plan_sin_messenger_es_409_con_su_codigo(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-plan", plan="free")
    meta([])
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/attempt", headers=headers)

    assert (r.status_code, r.json()["error"]["code"]) == (409, "plan_channel_not_included")
    assert "no incluye Messenger" in r.json()["error"]["message"]


async def test_un_id_de_pagina_con_formato_raro_es_400(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-id")
    meta([])
    headers = await api_headers(app_client, tid)

    for malo in ("123/subscribed_apps", "12 34", "abc", "1" * 40, "1?fields=x"):
        r = await app_client.post(
            f"{BASE}/connect", headers=headers, json={"connection_id": str(uuid.uuid4()), "page_ids": [malo]}
        )
        assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_id"), malo


async def test_sin_paginas_o_con_demasiadas_es_un_error_de_validacion(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-vacio")
    meta([])
    headers = await api_headers(app_client, tid)
    conexion = str(uuid.uuid4())

    vacia = await app_client.post(f"{BASE}/connect", headers=headers, json={"connection_id": conexion, "page_ids": []})
    muchas = await app_client.post(
        f"{BASE}/connect", headers=headers, json={"connection_id": conexion, "page_ids": [str(i) for i in range(51)]}
    )
    assert vacia.status_code == 422 and muchas.status_code == 422


async def test_una_conexion_que_no_existe_es_404(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-404")
    meta([])
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/connect", headers=headers, json={"connection_id": str(uuid.uuid4()), "page_ids": ["123"]})

    assert r.status_code == 404


async def test_el_codigo_y_el_intento_no_se_devuelven_en_un_error_de_validacion(app_client, meta) -> None:
    tid = await _new_tenant("api-msgr-secreto")
    meta([])
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/discover", headers=headers, json={"code": "secreto-del-codigo", "attempt": 123})

    assert r.status_code == 422
    assert "secreto-del-codigo" not in r.text
