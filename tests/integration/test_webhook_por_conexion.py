"""HU-CONN-05: webhook y secreto por conexión (ADR-0125).

Un negocio con dos conexiones de orígenes distintos (una BYOK con su app y otra de
Embedded Signup con la app de la plataforma) recibe de las dos: cada mensaje se verifica
con el secreto de SU conexión. "Registrar webhook" registra la app de esa conexión, y la
app de la plataforma solo la toca el master.

BD, Redis y la app reales; Meta nunca se llama (la suscripción de la app se falsea).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLATAFORMA_SECRETO = "plataforma-secret-conn05"
PLATAFORMA_APP = "app-plataforma-conn05"


@pytest.fixture(autouse=True)
def plataforma(monkeypatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_app_secret", PLATAFORMA_SECRETO)
    monkeypatch.setattr(settings, "meta_app_secret", PLATAFORMA_SECRETO)
    monkeypatch.setattr(settings, "meta_app_id", PLATAFORMA_APP)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "token-global-conn05")
    monkeypatch.setattr(settings, "app_env", "production")
    return settings


# ------------------------------------------------------------------ helpers
async def _negocio(prefix: str) -> tuple[uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de webhook por conexión.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL,
    )
    return tid, slug


async def _conexion(
    tid: uuid.UUID, *, origin: str, app_id: str, app_secret: str | None = None, verify_token: str | None = None,
) -> tuple[uuid.UUID, str, str]:
    """Conexión de Meta viva con su WABA y un número. Devuelve (id, waba, número)."""
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import APP_SECRET_KIND, WEBHOOK_VERIFY_TOKEN_KIND
    from core.shared.tenant import tenant_scope

    waba_id, phone = uniq("waba"), uniq("phone")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin=origin, meta_app_id=app_id, status="connected",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            session.add(
                PhoneNumber(tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone)
            )
            connection_id = connection.id
        if app_secret:
            await meta_token_store.put_token(connection_id, APP_SECRET_KIND, app_id, app_secret, tenant_id=tid)
        if verify_token:
            await meta_token_store.put_token(
                connection_id, WEBHOOK_VERIFY_TOKEN_KIND, app_id, verify_token, tenant_id=tid
            )
    return connection_id, waba_id, phone


async def _negocio_byok_y_es(prefix: str) -> dict:
    """El caso de la HU: una conexión BYOK (app propia) y otra de Embedded Signup."""
    tid, slug = await _negocio(prefix)
    byok_id, waba_byok, phone_byok = await _conexion(
        tid, origin="byok", app_id=uniq("app-byok"), app_secret="secreto-byok", verify_token="token-byok"
    )
    es_id, waba_es, phone_es = await _conexion(tid, origin="embedded_signup", app_id=PLATAFORMA_APP)
    return {
        "tid": tid, "slug": slug,
        "byok": (byok_id, waba_byok, phone_byok), "es": (es_id, waba_es, phone_es),
    }


def _entry(waba_id: str, phone: str, msg_id: str) -> dict:
    return {
        "id": waba_id,
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": phone},
                "contacts": [{"wa_id": "573001112233", "profile": {"name": "Cliente"}}],
                "messages": [{"from": "573001112233", "id": msg_id, "type": "text", "text": {"body": "hola"}}],
            },
        }],
    }


async def _post(app_client, entries: list[dict], secreto: str, *, slug: str | None = None):
    body = json.dumps({"object": "whatsapp_business_account", "entry": entries}).encode()
    firma = "sha256=" + hmac.new(secreto.encode(), body, hashlib.sha256).hexdigest()
    url = "/webhooks/whatsapp" + (f"/{slug}" if slug else "")
    return await app_client.post(
        url, content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": firma}
    )


async def _evento(external_id: str):
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == external_id))
        ).scalar_one_or_none()


# ------------------------------------------------------------------ Gherkin 1: convivencia
async def test_convivencia_byok_y_embedded_signup_cada_mensaje_con_el_secreto_de_su_conexion(app_client) -> None:
    """Dado un negocio con una conexión BYOK y una ES, cuando entran mensajes a ambos números,
    entonces cada uno se verifica con el secreto de su conexión y se procesa."""
    n = await _negocio_byok_y_es("conviven")
    _, waba_byok, phone_byok = n["byok"]
    _, waba_es, phone_es = n["es"]
    msg_byok, msg_es = f"wamid.{uniq('byok')}", f"wamid.{uniq('es')}"

    # La app BYOK entrega en la URL por slug, firmada con SU secreto.
    r_byok = await _post(app_client, [_entry(waba_byok, phone_byok, msg_byok)], "secreto-byok", slug=n["slug"])
    # La app de la plataforma entrega en el genérico, firmada con el de la plataforma.
    r_es = await _post(app_client, [_entry(waba_es, phone_es, msg_es)], PLATAFORMA_SECRETO)

    assert r_byok.status_code == 200 and r_byok.json()["enqueued"] == 1, r_byok.text
    assert r_es.status_code == 200 and r_es.json()["enqueued"] == 1, r_es.text
    for msg in (msg_byok, msg_es):
        fila = await _evento(msg)
        assert (fila.tenant_id, fila.process_error) == (n["tid"], None)


async def test_el_secreto_byok_no_autoriza_el_numero_de_la_conexion_de_la_plataforma(app_client) -> None:
    """El cuerpo no elige el secreto: el número de la conexión ES firmado con el secreto de la
    BYOK del mismo negocio no se procesa (antes, el secreto "del negocio" valía para todo)."""
    n = await _negocio_byok_y_es("cruzado")
    _, waba_byok, phone_byok = n["byok"]
    _, waba_es, phone_es = n["es"]
    msg_byok, msg_es = f"wamid.{uniq('byok')}", f"wamid.{uniq('es')}"

    solo_es = await _post(app_client, [_entry(waba_es, phone_es, f"wamid.{uniq('x')}")], "secreto-byok", slug=n["slug"])
    mezcla = await _post(
        app_client, [_entry(waba_byok, phone_byok, msg_byok), _entry(waba_es, phone_es, msg_es)], "secreto-byok",
    )

    assert solo_es.status_code == 401
    assert mezcla.status_code == 200 and mezcla.json()["enqueued"] == 1, mezcla.text
    assert (await _evento(msg_byok)).tenant_id == n["tid"]
    fila_es = await _evento(msg_es)
    assert (fila_es.tenant_id, fila_es.process_error, fila_es.signature_ok) == (
        None, "signature_connection_mismatch", False
    )


async def test_dos_apps_byok_del_mismo_negocio_reciben_las_dos(app_client) -> None:
    tid, slug = await _negocio("dos-apps")
    _, waba_1, phone_1 = await _conexion(tid, origin="byok", app_id=uniq("app-1"), app_secret="secreto-1")
    _, waba_2, phone_2 = await _conexion(tid, origin="byok", app_id=uniq("app-2"), app_secret="secreto-2")

    r1 = await _post(app_client, [_entry(waba_1, phone_1, f"wamid.{uniq('1')}")], "secreto-1", slug=slug)
    r2 = await _post(app_client, [_entry(waba_2, phone_2, f"wamid.{uniq('2')}")], "secreto-2", slug=slug)
    cruzado = await _post(app_client, [_entry(waba_2, phone_2, f"wamid.{uniq('c')}")], "secreto-1", slug=slug)

    assert r1.status_code == 200 and r1.json()["enqueued"] == 1, r1.text
    assert r2.status_code == 200 and r2.json()["enqueued"] == 1, r2.text
    assert cruzado.status_code == 401


async def test_un_evento_sin_numero_se_verifica_con_la_conexion_de_su_waba(app_client) -> None:
    """Plantillas y cuenta llegan sin número: deciden la WABA y su conexión."""
    tid, slug = await _negocio("solo-waba")
    _, waba, _ = await _conexion(tid, origin="byok", app_id=uniq("app-w"), app_secret="secreto-waba")
    entry = {"id": waba, "changes": [{"field": "account_update", "value": {"event": "VERIFIED_ACCOUNT"}}]}

    ok = await _post(app_client, [entry], "secreto-waba", slug=slug)
    malo = await _post(app_client, [entry], "otro-secreto", slug=slug)

    assert ok.status_code == 200, ok.text
    assert malo.status_code == 401


async def test_el_secreto_de_otro_negocio_no_autoriza_el_numero_de_este(app_client) -> None:
    """Aislamiento: X (BYOK) firma un POST con su número y el de T: solo se procesa el suyo."""
    n = await _negocio_byok_y_es("victima")
    _, waba_t, phone_t = n["byok"]
    tid_x, _ = await _negocio("atacante")
    _, waba_x, phone_x = await _conexion(tid_x, origin="byok", app_id=uniq("app-x"), app_secret="secreto-x")
    msg_t, msg_x = f"wamid.{uniq('t')}", f"wamid.{uniq('x')}"

    r = await _post(app_client, [_entry(waba_x, phone_x, msg_x), _entry(waba_t, phone_t, msg_t)], "secreto-x")
    por_slug = await _post(app_client, [_entry(waba_t, phone_t, f"wamid.{uniq('s')}")], "secreto-x", slug=n["slug"])

    assert r.status_code == 200 and r.json()["enqueued"] == 1, r.text
    assert (await _evento(msg_x)).tenant_id == tid_x
    fila_t = await _evento(msg_t)
    assert (fila_t.tenant_id, fila_t.process_error) == (None, "signature_tenant_mismatch")
    assert por_slug.status_code == 401


async def test_una_conexion_eliminada_no_presta_su_secreto(app_client) -> None:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, slug = await _negocio("eliminada")
    cid, waba, phone = await _conexion(tid, origin="byok", app_id=uniq("app-e"), app_secret="secreto-eliminada")
    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).deleted_at = utcnow()

    r = await _post(app_client, [_entry(waba, phone, f"wamid.{uniq('e')}")], "secreto-eliminada", slug=slug)

    assert r.status_code == 401


async def test_la_waba_de_una_conexion_no_presta_su_secreto_al_numero_de_otra(app_client) -> None:
    """El remitente no puede mezclar activos para elegir el secreto: la WABA de la BYOK con el
    número de la conexión ES (un par que no existe en Meta), firmado con el secreto de la
    BYOK, no se procesa como entrante del número ES (hallazgo de la revisión de seguridad)."""
    n = await _negocio_byok_y_es("mezcla")
    _, waba_byok, phone_byok = n["byok"]
    _, _, phone_es = n["es"]
    msg = f"wamid.{uniq('mezcla')}"

    por_slug = await _post(app_client, [_entry(waba_byok, phone_es, msg)], "secreto-byok", slug=n["slug"])
    generico = await _post(
        app_client,
        [_entry(waba_byok, phone_byok, f"wamid.{uniq('ok')}"), _entry(waba_byok, phone_es, msg)],
        "secreto-byok",
    )

    assert por_slug.status_code == 401
    assert generico.status_code == 200 and generico.json()["enqueued"] == 1, generico.text
    fila = await _evento(msg)
    assert (fila.tenant_id, fila.process_error, fila.signature_ok) == (None, "signature_connection_mismatch", False)


async def test_la_waba_de_una_byok_no_presta_su_secreto_al_numero_de_una_conexion_manual(app_client) -> None:
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    tid, slug = await _negocio("mezcla-manual")
    _, waba_byok, _ = await _conexion(tid, origin="byok", app_id=uniq("app-mm"), app_secret="secreto-byok-mm")
    phone_manual = f"57{uuid.uuid4().hex[:10]}"
    async with tenant_scope(tid):
        await save_connection(
            label="manual",
            credentials={"phone_number_id": phone_manual, "app_secret": "secreto-manual-mm", "verify_token": "vt"},
        )

    mezclado = await _post(app_client, [_entry(waba_byok, phone_manual, f"wamid.{uniq('m')}")], "secreto-byok-mm",
                           slug=slug)
    propio = await _post(app_client, [_entry(waba_byok, phone_manual, f"wamid.{uniq('p')}")], "secreto-manual-mm",
                         slug=slug)

    assert mezclado.status_code == 401
    assert propio.status_code == 200 and propio.json()["enqueued"] == 1, propio.text


async def test_sin_header_de_firma_401_sin_mirar_secretos(app_client, monkeypatch) -> None:
    import core.gateway.webhook_secrets as secretos_mod

    async def no_consultar(*_a, **_k):
        raise AssertionError("no se leen secretos sin una firma bien formada")

    monkeypatch.setattr(secretos_mod, "secretos_del_grupo", no_consultar)
    n = await _negocio_byok_y_es("sin-header")
    _, waba, phone = n["byok"]
    body = json.dumps({"object": "whatsapp_business_account", "entry": [_entry(waba, phone, "wamid.x")]}).encode()

    sin = await app_client.post(f"/webhooks/whatsapp/{n['slug']}", content=body)
    mala = await app_client.post(
        "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=nohex"}
    )

    assert sin.status_code == 401 and mala.status_code == 401


async def test_handshake_no_acepta_el_token_de_una_conexion_eliminada(app_client) -> None:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, slug = await _negocio("hs-eliminada")
    await _conexion(tid, origin="byok", app_id=uniq("app-viva"), verify_token="token-vivo")
    muerta, _, _ = await _conexion(tid, origin="byok", app_id=uniq("app-muerta"), verify_token="token-muerto")
    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, muerta)).deleted_at = utcnow()

    def _hs(token: str):
        return app_client.get(
            f"/webhooks/whatsapp/{slug}", params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "C"}
        )

    assert (await _hs("token-vivo")).status_code == 200
    assert (await _hs("token-muerto")).status_code == 403


# ------------------------------------------------------------------ Gherkin 2 y 3: registrar el webhook
async def _owner(tid: uuid.UUID, role: str = "owner") -> tuple[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email, password = f"{uniq('owner')}@example.test", "clave-segura-123"
    async with get_session() as session:
        owner = Admin(email=email, password_hash=hash_password(password), role="colaborador")
        session.add(owner)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=owner.id, role=role))
        await session.commit()
    return email, password


@pytest.fixture
def meta_falso(monkeypatch):
    """`upsert_app_subscription` sin Meta: guarda con qué app, URL y token se llamó."""
    import core.gateway.meta_subscriptions as subs_mod
    from core.gateway.meta_subscriptions import AppSubscriptionStatus

    llamadas: list[dict] = []

    async def _fake_upsert(config, object_, *, callback_url, verify_token):
        llamadas.append({
            "app_id": config.app_id, "app_secret": config.app_secret, "callback_url": callback_url,
            "verify_token": verify_token, "object": object_, "connection_id": config.connection_id,
        })
        return AppSubscriptionStatus(
            meta_app_id=config.app_id, object=object_, callback_url=callback_url, active=True,
            fields=["messages"], last_checked_at=None, last_error=None,
        )

    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _fake_upsert)
    return llamadas


async def test_registrar_webhook_byok_usa_la_app_de_esa_conexion(app_client, meta_falso) -> None:
    """Dado "Registrar webhook" en una conexión BYOK, se registra en la app de esa conexión con
    su URL y su token de verificación, sin tocar la app de la plataforma."""
    from core.tenants import tenant_webhook_url

    n = await _negocio_byok_y_es("reg-byok")
    byok_id = n["byok"][0]
    email, password = await _owner(n["tid"])
    headers = await api_headers(app_client, n["tid"], email=email, password=password)

    r = await app_client.post(f"/api/v1/admin/meta/connections/{byok_id}/webhook/register", headers=headers)

    assert r.status_code == 204, r.text
    (llamada,) = meta_falso
    assert llamada["app_secret"] == "secreto-byok"
    assert llamada["app_id"] != PLATAFORMA_APP
    assert llamada["callback_url"] == tenant_webhook_url(n["slug"])
    assert llamada["verify_token"] == "token-byok"
    assert llamada["connection_id"] == byok_id
    # Y el handshake de esa URL acepta el token registrado.
    hs = await app_client.get(
        f"/webhooks/whatsapp/{n['slug']}",
        params={"hub.mode": "subscribe", "hub.verify_token": "token-byok", "hub.challenge": "OK"},
    )
    assert hs.status_code == 200 and hs.text == "OK"


async def test_registrar_webhook_byok_sin_secreto_no_cae_a_la_app_de_la_plataforma(app_client, meta_falso) -> None:
    tid, _ = await _negocio("reg-sin")
    cid, _, _ = await _conexion(tid, origin="byok", app_id=uniq("app-sin"))
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"/api/v1/admin/meta/connections/{cid}/webhook/register", headers=headers)

    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_app_secret", r.text
    assert meta_falso == []


async def test_registrar_webhook_de_la_plataforma_solo_lo_hace_el_master(app_client, meta_falso) -> None:
    """Dado una conexión ES, re-registrar el webhook (la app de la plataforma) solo lo puede
    hacer el master."""
    n = await _negocio_byok_y_es("reg-es")
    es_id = n["es"][0]
    email, password = await _owner(n["tid"])
    owner = await api_headers(app_client, n["tid"], email=email, password=password)
    master = await api_headers(app_client, n["tid"])

    r_owner = await app_client.post(f"/api/v1/admin/meta/connections/{es_id}/webhook/register", headers=owner)
    legacy = await app_client.post(
        "/api/v1/admin/meta/webhooks/register", json={"connection_id": str(es_id)}, headers=owner
    )
    assert r_owner.status_code == 403 and r_owner.json()["error"]["code"] == "platform_app_master_only", r_owner.text
    assert legacy.status_code == 403
    assert meta_falso == []

    r_master = await app_client.post(f"/api/v1/admin/meta/connections/{es_id}/webhook/register", headers=master)
    assert r_master.status_code == 204, r_master.text
    (llamada,) = meta_falso
    assert llamada["app_id"] == PLATAFORMA_APP
    assert llamada["callback_url"].endswith("/webhooks/whatsapp")
    assert llamada["verify_token"] == "token-global-conn05"


async def test_registrar_webhook_de_una_conexion_de_otro_negocio_da_404(app_client, meta_falso) -> None:
    n = await _negocio_byok_y_es("reg-ajeno")
    tid_x, _ = await _negocio("reg-ajeno-x")
    email, password = await _owner(tid_x)
    headers = await api_headers(app_client, tid_x, email=email, password=password)

    r = await app_client.post(f"/api/v1/admin/meta/connections/{n['byok'][0]}/webhook/register", headers=headers)

    assert r.status_code == 404
    assert meta_falso == []


async def test_la_ruta_vieja_por_canal_pide_la_conexion_si_hay_varias(app_client, meta_falso) -> None:
    n = await _negocio_byok_y_es("reg-vieja")
    headers = await api_headers(app_client, n["tid"])

    r = await app_client.post("/api/v1/admin/meta/webhooks/register", json={"channel": "whatsapp"}, headers=headers)

    assert r.status_code == 400 and r.json()["error"]["code"] == "connection_required", r.text
    assert meta_falso == []


async def test_el_listado_dice_quien_puede_registrar_y_oculta_la_app_de_la_plataforma(app_client) -> None:
    from core.gateway.meta_subscriptions import persist_subscription_state

    n = await _negocio_byok_y_es("listado")
    byok_id, es_id = n["byok"][0], n["es"][0]
    await persist_subscription_state(
        PLATAFORMA_APP, "whatsapp_business_account", callback_url="https://plataforma.test/webhooks/whatsapp",
        active=True, fields=["messages"], last_error="detalle interno de la plataforma",
    )
    email, password = await _owner(n["tid"])
    owner = await api_headers(app_client, n["tid"], email=email, password=password)
    master = await api_headers(app_client, n["tid"])

    filas_owner = {
        f["connection_id"]: f for f in (await app_client.get("/api/v1/admin/meta/webhooks", headers=owner)).json()[
            "subscriptions"
        ]
    }
    filas_master = {
        f["connection_id"]: f for f in (await app_client.get("/api/v1/admin/meta/webhooks", headers=master)).json()[
            "subscriptions"
        ]
    }

    byok, es = filas_owner[str(byok_id)], filas_owner[str(es_id)]
    assert (byok["origin"], byok["uses_platform_app"], byok["can_register"]) == ("byok", False, True)
    assert (es["origin"], es["uses_platform_app"], es["can_register"]) == ("embedded_signup", True, False)
    assert es["callback_url"] is None and es["last_error"] is None
    assert es["active"] is True
    es_master = filas_master[str(es_id)]
    assert es_master["can_register"] is True
    assert es_master["callback_url"] == "https://plataforma.test/webhooks/whatsapp"


async def test_un_colaborador_no_registra_el_webhook(app_client, meta_falso) -> None:
    n = await _negocio_byok_y_es("reg-colab")
    email, password = await _owner(n["tid"], role="colaborador")
    headers = await api_headers(app_client, n["tid"], email=email, password=password)

    r = await app_client.post(f"/api/v1/admin/meta/connections/{n['byok'][0]}/webhook/register", headers=headers)

    assert r.status_code == 403
    assert meta_falso == []


async def test_una_byok_con_la_app_de_la_plataforma_solo_la_registra_el_master(app_client, meta_falso) -> None:
    """Excepción a ADR-0094 §1 (ADR-0125): una "BYOK" con el `META_APP_ID` de la plataforma
    movería el callback de la app compartida a su slug."""
    tid, _ = await _negocio("reg-byok-plat")
    cid, _, _ = await _conexion(tid, origin="byok", app_id=PLATAFORMA_APP, app_secret=PLATAFORMA_SECRETO)
    email, password = await _owner(tid)
    headers = await api_headers(app_client, tid, email=email, password=password)

    r = await app_client.post(f"/api/v1/admin/meta/connections/{cid}/webhook/register", headers=headers)

    assert r.status_code == 403 and r.json()["error"]["code"] == "platform_app_master_only"
    assert meta_falso == []


async def test_la_ruta_vieja_con_solo_una_conexion_de_la_plataforma_no_la_registra_el_dueno(
    app_client, meta_falso
) -> None:
    tid, _ = await _negocio("reg-vieja-es")
    await _conexion(tid, origin="embedded_signup", app_id=PLATAFORMA_APP)
    email, password = await _owner(tid)
    headers = await api_headers(app_client, tid, email=email, password=password)

    r = await app_client.post("/api/v1/admin/meta/webhooks/register", json={"channel": "whatsapp"}, headers=headers)

    assert r.status_code == 403 and r.json()["error"]["code"] == "platform_app_master_only"
    assert meta_falso == []


async def test_registrar_una_conexion_pausada_pide_activarla(app_client, meta_falso) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _ = await _negocio("reg-pausada")
    cid, _, _ = await _conexion(tid, origin="byok", app_id=uniq("app-p"), app_secret="secreto-p")
    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).status = "revoked"
    headers = await api_headers(app_client, tid)

    r = await app_client.post(f"/api/v1/admin/meta/connections/{cid}/webhook/register", headers=headers)

    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_state"
    assert meta_falso == []


async def test_el_listado_no_muestra_la_url_de_otro_negocio_en_una_app_byok_compartida(app_client) -> None:
    from core.gateway.meta_subscriptions import persist_subscription_state

    tid, _ = await _negocio("lst-compartida")
    app_id = uniq("app-compartida")
    cid, _, _ = await _conexion(tid, origin="byok", app_id=app_id, app_secret="s")
    await persist_subscription_state(
        app_id, "whatsapp_business_account", callback_url="https://x.test/webhooks/whatsapp/otro-negocio",
        active=True, fields=["messages"], last_error="error de otro negocio",
    )
    headers = await api_headers(app_client, tid)

    (fila,) = [
        f for f in (await app_client.get("/api/v1/admin/meta/webhooks", headers=headers)).json()["subscriptions"]
        if f["connection_id"] == str(cid)
    ]

    assert fila["callback_url"] is None and fila["last_error"] is None
