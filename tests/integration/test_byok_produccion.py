"""BYOK de producción: entorno del wizard, registro con PIN y promoción (HU-CONN-06, ADR-0127).

Con la BD y Redis reales; Graph, falso (nunca la API real de Meta). Cubre los cuatro
criterios de la HU:

- el wizard BYOK con "Producción" guarda `environment = production`;
- un número de producción sin registrar queda `pending` y el PIN lo deja `connected`;
- "Pasar a producción" de una BYOK de prueba: número real, registro, plantillas y, al
  terminar, el entorno cambia;
- los chequeos de producción de la conexión usan la app DEL NEGOCIO, no la de la plataforma;

y el aislamiento entre negocios de las rutas nuevas.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLATFORM_SECRET = "secreto-de-la-plataforma"


@dataclass
class GraphFalso:
    """Graph API de mentira: números (con `platform_type`/`account_mode`), WABAs, registro y
    suscripción de la WABA a una app. Anota cada llamada con el App Secret que usó."""

    numeros: dict[str, dict] = field(default_factory=dict)
    wabas: dict[str, list[str]] = field(default_factory=dict)
    suscrita_a: dict[str, str] = field(default_factory=dict)
    app_id: str = ""
    llamadas: list[tuple[str, str, str | None, dict | None]] = field(default_factory=list)

    async def fetch(self, path, *, method="GET", body=None, app_secret=None, query=None, **_kwargs):
        self.llamadas.append((method, path, app_secret, body))
        partes = path.strip("/").split("/")
        if len(partes) == 2 and partes[1] == "register" and method == "POST":
            self.numeros[partes[0]]["platform_type"] = "CLOUD_API"
            return {"success": True}
        if len(partes) == 2 and partes[1] == "phone_numbers":
            return {
                "data": [{"id": pnid, **self.numeros[pnid]} for pnid in self.wabas.get(partes[0], [])],
                "paging": {"cursors": {}},
            }
        if len(partes) == 2 and partes[1] == "subscribed_apps":
            if method == "POST":
                self.suscrita_a[partes[0]] = self.app_id
                return {"success": True}
            app = self.suscrita_a.get(partes[0])
            return {"data": [{"whatsapp_business_api_data": {"id": app}}] if app else []}
        if len(partes) == 1 and partes[0] in self.numeros:
            return {"id": partes[0], **self.numeros[partes[0]]}
        raise AssertionError(f"Graph no esperado: {method} {path}")


def _instalar(monkeypatch, graph: GraphFalso, tokens_validados: list | None = None) -> list:
    """Pone el Graph falso en todos los módulos que llaman a Meta en estos flujos."""
    import core.gateway.meta_byok as byok_mod
    import core.gateway.meta_byok_promocion as promo_mod
    import core.gateway.meta_graph as graph_mod
    import core.gateway.meta_health as health_mod
    import core.gateway.meta_phone_numbers as phones_mod
    import core.gateway.meta_subscriptions as subs_mod
    from core.config import get_settings
    from core.gateway.meta_preflight import PreflightCheck

    for mod in (byok_mod, promo_mod, graph_mod, phones_mod):
        monkeypatch.setattr(mod, "graph_fetch", graph.fetch)

    async def _sin_vencimiento(*_args):
        return None

    monkeypatch.setattr(byok_mod, "_token_expiry", _sin_vencimiento)

    registros: list = []

    async def _upsert(config, object_, *, callback_url, verify_token):
        registros.append((config.app_id, config.app_secret, callback_url))
        return object()

    monkeypatch.setattr(subs_mod, "upsert_app_subscription", _upsert)

    validados = tokens_validados if tokens_validados is not None else []

    async def _token_valido(token, app_id, app_secret, version, **_kwargs):
        validados.append((app_id, app_secret))
        return PreflightCheck(key="token_valid", ok=True, detail="Token válido")

    monkeypatch.setattr(health_mod, "check_token_valid", _token_valido)
    # La app de la plataforma existe en este entorno: nada de lo de abajo debe usarla.
    monkeypatch.setattr(get_settings(), "meta_app_id", "app-de-la-plataforma")
    monkeypatch.setattr(get_settings(), "meta_app_secret", PLATFORM_SECRET)
    return registros


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("byok-prod")
    tid, _ = await onboard_tenant(
        name=f"BYOK prod {slug}", slug=slug, persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL
    )
    return tid


async def _conexion(tid: uuid.UUID):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.deleted_at.is_(None))
                )
            ).scalar_one()


def _alta(app_id: str, waba_id: str, phone_id: str, **extra) -> dict:
    return {
        "app_id": app_id,
        "app_secret": "secreto-del-negocio",
        "phone_number_id": phone_id,
        "access_token": "tok-del-negocio",
        "waba_id": waba_id,
        **extra,
    }


# --------------------------------------------------------------- criterio 1 y 2: el wizard
async def test_wizard_produccion_guarda_el_entorno(app_client, monkeypatch) -> None:
    tid = await _negocio()
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "CLOUD_API", "account_mode": "LIVE", "display_phone_number": "+57 1"}
    registros = _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok",
        json=_alta(app_id, waba_id, phone_id, environment="production"),
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 201, r.text
    assert r.json()["environment"] == "production"
    assert r.json()["status"] == "connected"
    assert r.json()["needsRegistration"] is False
    connection = await _conexion(tid)
    assert connection.environment == "production"
    assert connection.status == "connected"
    # La suscripción se registra en SU app, con SU secreto.
    assert registros and registros[0][:2] == (app_id, "secreto-del-negocio")
    assert all(secret != PLATFORM_SECRET for _m, _p, secret, _b in graph.llamadas)


async def test_wizard_sin_entorno_sigue_siendo_prueba(app_client, monkeypatch) -> None:
    tid = await _negocio()
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "CLOUD_API", "account_mode": "SANDBOX"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(app_id, waba_id, phone_id),
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 201, r.text
    assert (await _conexion(tid)).environment == "sandbox"


async def test_wizard_produccion_rechaza_el_numero_de_prueba(app_client, monkeypatch) -> None:
    tid = await _negocio()
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "CLOUD_API", "account_mode": "SANDBOX"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok",
        json=_alta(app_id, waba_id, phone_id, environment="production"),
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 400
    assert "prueba" in r.json()["error"]["message"]


async def test_wizard_rechaza_la_app_de_la_plataforma(app_client, monkeypatch) -> None:
    tid = await _negocio()
    graph = GraphFalso()
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok",
        json=_alta("app-de-la-plataforma", uniq("waba"), uniq("ph"), environment="production"),
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 400
    assert graph.llamadas == []


async def test_numero_sin_registrar_queda_pendiente_y_el_pin_lo_conecta(app_client, monkeypatch) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "NOT_APPLICABLE", "account_mode": "LIVE"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok",
        json=_alta(app_id, waba_id, phone_id, environment="production"),
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    assert r.json()["needsRegistration"] is True
    connection = await _conexion(tid)
    assert connection.status == "pending"

    # El paso del PIN: la ruta de "Números", la misma para BYOK y manuales.
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{connection.id}/numbers/{phone_id}/register",
        json={"pin": "135790"},
        headers=headers,
    )
    assert r.status_code == 204, r.text
    assert ("POST", f"/{phone_id}/register", "secreto-del-negocio", {"messaging_product": "whatsapp", "pin": "135790"}) in graph.llamadas
    connection = await _conexion(tid)
    assert connection.status == "connected"
    assert connection.status_reason is None


async def test_el_pin_en_el_wizard_registra_y_conecta(app_client, monkeypatch) -> None:
    tid = await _negocio()
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "NOT_APPLICABLE", "account_mode": "LIVE"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok",
        json=_alta(app_id, waba_id, phone_id, environment="production", pin="246802"),
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 201, r.text
    assert r.json()["status"] == "connected"
    assert r.json()["needsRegistration"] is False
    assert "246802" not in r.text
    assert (await _conexion(tid)).status == "connected"


async def test_un_pin_rechazado_deja_la_conexion_pendiente_con_el_motivo(app_client, monkeypatch) -> None:
    from core.gateway.meta_errors import GraphError

    tid = await _negocio()
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "NOT_APPLICABLE", "account_mode": "LIVE"}
    _instalar(monkeypatch, graph)
    original = graph.fetch

    async def _pin_errado(path, *, method="GET", **kwargs):
        if path.endswith("/register"):
            raise GraphError("PIN mismatch", category="PERMANENT", code=133005, http_status=400)
        return await original(path, method=method, **kwargs)

    import core.gateway.meta_phone_numbers as phones_mod

    monkeypatch.setattr(phones_mod, "graph_fetch", _pin_errado)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok",
        json=_alta(app_id, waba_id, phone_id, environment="production", pin="111111"),
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    assert r.json()["needsRegistration"] is True
    assert "otro PIN" in r.json()["registrationError"]


# ------------------------------------------------- criterio 3 y 4: pasar a producción
def _num() -> str:
    """ID de Graph de mentira (solo dígitos, como los de Meta)."""
    return str(uuid.uuid4().int)[:15]


async def _byok_de_prueba(tid: uuid.UUID) -> tuple[uuid.UUID, str, str]:
    """Conexión BYOK de prueba con su número de prueba de Meta, lista (secreto, token y
    `verify_token`)."""
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_byok, meta_token_store
    from core.shared.tenant import tenant_scope

    app_id, waba_id, phone_id = uniq("app"), _num(), _num()
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_id,
                status="connected",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id, subscribed=True)
            session.add(waba)
            await session.flush()
            session.add(
                PhoneNumber(
                    tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone_id,
                    registered=True, raw={"account_mode": "SANDBOX"},
                )
            )
            cid = connection.id
        await meta_token_store.put_token(cid, meta_byok.APP_SECRET_KIND, app_id, "secreto-del-negocio", tenant_id=tid)
        await meta_token_store.put_token(cid, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, app_id, "tok-prueba", tenant_id=tid)
        await meta_token_store.put_token(
            cid, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, app_id, "verify-de-la-prueba-0123456789", tenant_id=tid
        )
    return cid, app_id, phone_id


async def _fila(tid: uuid.UUID, cid: uuid.UUID):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await session.get(MetaConnection, cid)


async def _plantilla_aprobada(tid: uuid.UUID, phone_number_id: str) -> None:
    from core.db.models import MessageTemplate, PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            phone = (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.tenant_id == tid,
                        PhoneNumber.phone_number_id == phone_number_id,
                        PhoneNumber.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            session.add(
                MessageTemplate(
                    tenant_id=tid, waba_account_id=phone.waba_account_id, name=uniq("bienvenida"), language="es",
                    category="UTILITY", status="APPROVED", components=[],
                )
            )


def _url(cid, sufijo: str = "") -> str:
    return f"/api/v1/admin/meta/connections/{cid}/promotion{sufijo}"


async def test_pasar_a_produccion_guia_el_cambio_y_al_terminar_cambia_el_entorno(app_client, monkeypatch) -> None:
    from core.db.models import Conversation, PhoneNumber
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import WEBHOOK_VERIFY_TOKEN_KIND
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    cid, app_id, phone_prueba = await _byok_de_prueba(tid)
    waba_real, phone_real = _num(), _num()
    graph = GraphFalso(app_id=app_id, wabas={waba_real: [phone_real]})
    graph.numeros[phone_real] = {
        "platform_type": "NOT_APPLICABLE", "account_mode": "LIVE", "display_phone_number": "+57 300 000",
    }
    tokens_validados: list = []
    registros = _instalar(monkeypatch, graph, tokens_validados)

    estado = await app_client.get(_url(cid), headers=headers)
    assert estado.status_code == 200, estado.text
    assert estado.json()["environment"] == "sandbox"
    assert estado.json()["blocked_reason"] is None
    assert estado.json()["production_connection_id"] is None
    assert [p["done"] for p in estado.json()["steps"]] == [False, False, False, False]

    # Paso 1: número real, con el token permanente del negocio. Crea la conexión de producción.
    r = await app_client.post(
        _url(cid, "/number"),
        json={"waba_id": waba_real, "phone_number_id": phone_real, "access_token": "tok-system-user"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert "tok-system-user" not in r.text
    assert r.json()["phone_number_id"] == phone_real
    assert [p["done"] for p in r.json()["steps"]] == [True, False, False, False]
    assert r.json()["production_status"] == "pending"
    prod_id = uuid.UUID(r.json()["production_connection_id"])
    assert prod_id != cid
    assert graph.suscrita_a[waba_real] == app_id
    # La app y su secreto son los de la conexión de prueba, y su `verify_token` no cambia.
    assert registros[-1][:2] == (app_id, "secreto-del-negocio")
    async with tenant_scope(tid):
        assert (
            await meta_token_store.get_token(prod_id, WEBHOOK_VERIFY_TOKEN_KIND, app_id, tenant_id=tid)
            == "verify-de-la-prueba-0123456789"
        )
    # La de prueba sigue igual mientras tanto (sus testers siguen funcionando).
    prueba = await _fila(tid, cid)
    assert (prueba.environment, prueba.status, prueba.deleted_at) == ("sandbox", "connected", None)

    # Sin registro ni plantillas, terminar no cambia nada.
    r = await app_client.post(_url(cid, "/finish"), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["promoted"] is False
    fallan = {c["key"] for c in r.json()["checks"] if c["ok"] is False}
    assert {"number_registered", "templates"} <= fallan
    assert (await _fila(tid, cid)).deleted_at is None

    # Paso 2: registro con el PIN, en la conexión de producción.
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{prod_id}/numbers/{phone_real}/register", json={"pin": "975310"},
        headers=headers,
    )
    assert r.status_code == 204, r.text
    assert (await _fila(tid, prod_id)).status == "connected"

    # Paso 3: una plantilla aprobada en la WABA del número real.
    await _plantilla_aprobada(tid, phone_real)

    # Una conversación de un tester con la conexión de prueba.
    async with tenant_scope(tid):
        async with get_session() as session:
            from core.db.models import Customer

            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:+1555{_num()[:7]}")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp", meta_connection_id=cid)
            session.add(conv)
            await session.flush()
            conv_id = conv.id

    # Paso 4: verificar con la app del negocio y pasar a producción (pasado el límite de 10 s).
    from core.memory.working import get_redis

    await get_redis().delete(f"promocion_chequeos:{tid}:{cid}")
    tokens_validados.clear()
    graph.llamadas.clear()
    r = await app_client.post(_url(cid, "/finish"), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["promoted"] is True, r.json()["checks"]
    assert r.json()["state"]["connection_id"] == str(prod_id)
    assert r.json()["state"]["environment"] == "production"
    assert all(p["done"] for p in r.json()["state"]["steps"])
    produccion = await _fila(tid, prod_id)
    assert (produccion.environment, produccion.status) == ("production", "connected")
    prueba = await _fila(tid, cid)
    assert prueba.deleted_at is not None  # la de prueba queda dada de baja (borrado lógico)
    async with tenant_scope(tid):
        async with get_session() as session:
            numero_prueba = (
                await session.execute(
                    select(PhoneNumber).where(PhoneNumber.tenant_id == tid, PhoneNumber.phone_number_id == phone_prueba)
                )
            ).scalar_one()
            conversacion = await session.get(Conversation, conv_id)
    assert numero_prueba.deleted_at is not None
    # La conversación del tester sigue con la de prueba (en pausa): nunca sale por el número real.
    assert conversacion.meta_connection_id == cid

    # Criterio 4: los chequeos usaron la app del negocio, nunca la de la plataforma.
    assert tokens_validados == [(app_id, "secreto-del-negocio")]
    assert graph.llamadas and all(secret == "secreto-del-negocio" for _m, _p, secret, _b in graph.llamadas)
    own_app = next(c for c in r.json()["checks"] if c["key"] == "own_app")
    assert own_app["ok"] is True and app_id in own_app["detail"]

    # La de prueba ya no existe: ni se vuelve a promover ni se registran sus números.
    assert (await app_client.post(_url(cid, "/finish"), headers=headers)).status_code == 404
    graph.llamadas.clear()
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{cid}/numbers/{phone_prueba}/register", json={"pin": "123456"},
        headers=headers,
    )
    assert r.status_code == 400
    assert graph.llamadas == []


async def test_una_byok_creada_en_produccion_se_verifica_con_su_app(app_client, monkeypatch) -> None:
    """Criterio 4: una BYOK de producción (alta directa del wizard) corre los chequeos con su
    app; una WABA suscrita a otra app falla."""
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    app_id, waba_id, phone_id = uniq("app"), _num(), _num()
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "CLOUD_API", "account_mode": "LIVE"}
    tokens_validados: list = []
    _instalar(monkeypatch, graph, tokens_validados)
    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(app_id, waba_id, phone_id, environment="production"),
        headers=headers,
    )
    assert r.status_code == 201, r.text
    cid = r.json()["connectionId"]

    graph.suscrita_a[waba_id] = "otra-app"
    r = await app_client.post(_url(cid, "/verify"), headers=headers)

    assert r.status_code == 200, r.text
    checks = {c["key"]: c for c in r.json()["checks"]}
    assert checks["own_app"]["ok"] is True
    assert checks["real_number"]["ok"] is True
    assert checks["waba_subscribed_to_own_app"]["ok"] is False
    assert tokens_validados == [(app_id, "secreto-del-negocio")]
    estado = await app_client.get(_url(cid), headers=headers)
    assert estado.json()["environment"] == "production"
    assert estado.json()["production_connection_id"] == cid


async def test_numero_de_prueba_no_sirve_como_numero_real(app_client, monkeypatch) -> None:
    tid = await _negocio()
    cid, app_id, _ = await _byok_de_prueba(tid)
    waba, phone = _num(), _num()
    graph = GraphFalso(app_id=app_id, wabas={waba: [phone]})
    graph.numeros[phone] = {"platform_type": "CLOUD_API", "account_mode": "SANDBOX"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        _url(cid, "/number"), json={"waba_id": waba, "phone_number_id": phone},
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "test_number"


async def test_los_ids_del_numero_real_son_solo_digitos(app_client, monkeypatch) -> None:
    tid = await _negocio()
    cid, app_id, _ = await _byok_de_prueba(tid)
    graph = GraphFalso(app_id=app_id)
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        _url(cid, "/number"), json={"waba_id": "123/subscribed_apps", "phone_number_id": "456"},
        headers=await api_headers(app_client, tid),
    )

    assert r.status_code == 422
    assert graph.llamadas == []


async def test_el_numero_real_de_otro_negocio_no_se_toma(app_client, monkeypatch) -> None:
    from core.db.models import MetaConnection, PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid_a, tid_b = await _negocio(), await _negocio()
    waba, phone = _num(), _num()
    async with tenant_scope(tid_a):
        async with get_session() as session:
            otra = MetaConnection(
                tenant_id=tid_a, channel="whatsapp", environment="production", origin="byok", meta_app_id=uniq("app"),
                status="connected",
            )
            session.add(otra)
            await session.flush()
            session.add(PhoneNumber(tenant_id=tid_a, connection_id=otra.id, phone_number_id=phone))
    cid_b, app_b, _ = await _byok_de_prueba(tid_b)
    graph = GraphFalso(app_id=app_b, wabas={waba: [phone]})
    graph.numeros[phone] = {"platform_type": "CLOUD_API", "account_mode": "LIVE"}
    _instalar(monkeypatch, graph)

    r = await app_client.post(
        _url(cid_b, "/number"), json={"waba_id": waba, "phone_number_id": phone},
        headers=await api_headers(app_client, tid_b),
    )

    assert r.status_code == 400
    async with tenant_scope(tid_b):
        async with get_session() as session:
            produccion = (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid_b,
                        MetaConnection.environment == "production",
                        MetaConnection.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
    assert produccion is None


async def test_otro_negocio_no_ve_ni_promueve_la_conexion(app_client, monkeypatch) -> None:
    tid_a = await _negocio()
    tid_b = await _negocio()
    cid_a, app_id, _ = await _byok_de_prueba(tid_a)
    graph = GraphFalso(app_id=app_id)
    _instalar(monkeypatch, graph)
    headers_b = await api_headers(app_client, tid_b)

    assert (await app_client.get(_url(cid_a), headers=headers_b)).status_code == 404
    r = await app_client.post(
        _url(cid_a, "/number"), json={"waba_id": _num(), "phone_number_id": _num()}, headers=headers_b
    )
    assert r.status_code == 404
    assert (await app_client.post(_url(cid_a, "/verify"), headers=headers_b)).status_code == 404
    assert (await app_client.post(_url(cid_a, "/finish"), headers=headers_b)).status_code == 404
    assert graph.llamadas == []
    prueba = await _fila(tid_a, cid_a)
    assert (prueba.environment, prueba.deleted_at) == ("sandbox", None)


async def _colaborador(tid: uuid.UUID) -> tuple[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colaborador')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
    return email, "clave-segura-123"


async def test_un_colaborador_no_da_de_alta_produccion_ni_promueve(app_client, monkeypatch) -> None:
    tid = await _negocio()
    cid, app_id, _ = await _byok_de_prueba(tid)
    graph = GraphFalso(app_id=app_id)
    _instalar(monkeypatch, graph)
    email, clave = await _colaborador(tid)
    headers = await api_headers(app_client, tid, email=email, password=clave)

    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(uniq("app"), _num(), _num(), environment="production"),
        headers=headers,
    )
    assert r.status_code == 403
    r = await app_client.post(
        _url(cid, "/number"), json={"waba_id": _num(), "phone_number_id": _num()}, headers=headers
    )
    assert r.status_code == 403
    assert (await app_client.post(_url(cid, "/finish"), headers=headers)).status_code == 403
    assert graph.llamadas == []


async def test_solo_byok_se_promueve(app_client, monkeypatch) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="embedded_signup",
                meta_app_id="app-de-la-plataforma", status="connected",
            )
            session.add(connection)
            await session.flush()
            cid = connection.id
    _instalar(monkeypatch, GraphFalso())

    r = await app_client.post(_url(cid, "/finish"), headers=await api_headers(app_client, tid))

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "not_byok"


def test_el_motivo_de_registro_pendiente_es_un_contrato_con_las_filas_guardadas() -> None:
    """`esperando_registro` compara `status_reason` con este texto exacto: cambiarlo exige
    actualizar las filas `pending` guardadas (ADR-0127)."""
    from core.gateway.meta_connection_state import MOTIVO_NUMERO_SIN_REGISTRAR

    assert MOTIVO_NUMERO_SIN_REGISTRAR == "Falta registrar el número en la Cloud API con su PIN"


async def test_produccion_pide_el_pin_explicito(app_client, monkeypatch) -> None:
    """El número real del negocio no recibe un PIN generado que nadie ve (ADR-0127)."""
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    app_id, waba_id, phone_id = uniq("app"), _num(), _num()
    graph = GraphFalso(app_id=app_id, wabas={waba_id: [phone_id]})
    graph.numeros[phone_id] = {"platform_type": "NOT_APPLICABLE", "account_mode": "LIVE"}
    _instalar(monkeypatch, graph)
    r = await app_client.post(
        "/api/v1/admin/meta/wizard/byok", json=_alta(app_id, waba_id, phone_id, environment="production"),
        headers=headers,
    )
    cid = r.json()["connectionId"]

    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{cid}/numbers/{phone_id}/register", json={}, headers=headers
    )

    assert r.status_code == 400
    assert "PIN" in r.json()["error"]["message"]
    assert not [c for c in graph.llamadas if c[1].endswith("/register")]
