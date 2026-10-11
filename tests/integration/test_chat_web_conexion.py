"""HU-AOS-11 parte 2 · el chat web como conexión `webchat` y altas posteriores (ADR-0115 §4 y §6).

- **Configuración → Chat web** guarda en `app_settings` (como antes) y en la conexión `webchat`
  del negocio, en la misma transacción: clave aleatoria generada por el servidor, secreto de
  Turnstile cifrado en `credentials_encrypted`, dominios en `settings`. La conexión no es manual:
  no se lista, no se edita por la API de conexiones y no le quita la primacía a nadie.
- **Endpoint público** por la clave (`webchat_resolve`, SECURITY DEFINER): resuelve la conexión
  viva, la del negocio correcto, sin exponer el secreto; cae a `app_settings` solo si el negocio
  nunca tuvo conexión; el polling filtra por conexión (dos widgets no se ven entre sí).
- **Backfill** idempotente con dry-run: el widget de `app_settings` queda como conexión y las
  conversaciones web sin conexión la reciben; el backfill de agentes lo exige antes.
- **Ruteo:** con la bandera apagada, el camino de siempre (la conversación web toma la conexión);
  encendida, adopta la conversación web sin conexión del visitante.
- **Altas posteriores:** widget, conexión manual, `waweb` y números nuevos de `sync_phone_numbers`
  nacen con Coordinador y General si el negocio ya tiene agentes.
- **Ruteo por conexión de punta a punta** con un webhook de Meta firmado (dos números de la misma
  cuenta): una conversación por número y la respuesta sale por el número al que escribió.

Sin LLM ni red.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_api_registrar_numero import _negocio_con_byok
from test_backfill_agentes import _modules, _sql
from test_byok_produccion import _negocio
from test_conexion_canonica import _alta_numero, _bandera, _conexiones
from test_instancias_por_conexion_api import _con_agentes, _instancias

pytestmark = [
    pytest.mark.aislamiento,  # resolución pública por clave entre negocios (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

ORIGEN = "https://tienda.example"
SETTINGS = "/api/v1/admin/settings/web-chat"


def _publico(key: str, sufijo: str) -> str:
    return f"/api/v1/public/web-chat/{key}/{sufijo}"


# ---------------------------------------------------------------- helpers
async def _guardar(app_client, tid: uuid.UUID, **cambios: Any) -> dict[str, Any]:
    cuerpo = {"enabled": True, "allowed_origins": [ORIGEN]} | cambios
    r = await app_client.put(SETTINGS, headers=await api_headers(app_client, tid), json=cuerpo)
    assert r.status_code == 200, r.text
    return r.json()


async def _webchat(tid: uuid.UUID, *, todas: bool = False) -> list[Any]:
    return await _sql(
        tid,
        "select id, external_id, active, is_primary, settings, credentials_encrypted, deleted_at, "
        "meta_connection_id from channel_connections where tenant_id = :t and type_id = 'webchat' "
        + ("" if todas else "and deleted_at is null ")
        + "order by created_at",
        t=tid,
    )


async def _config_legacy(tid: uuid.UUID, config: dict[str, Any]) -> None:
    """Un negocio que guardó el widget antes del despliegue: solo `app_settings`."""
    await _sql(
        tid,
        "insert into app_settings (id, tenant_id, web_chat_config) values (:id, :t, cast(:c as jsonb)) "
        "on conflict (tenant_id) where deleted_at is null do update set web_chat_config = excluded.web_chat_config",
        id=uuid.uuid4(), t=tid, c=json.dumps(config),
    )


async def _conversacion_web(tid: uuid.UUID, visitante: str, *, connection_id: uuid.UUID | None = None) -> uuid.UUID:
    cliente, conversacion = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid,
        "insert into customers (id, tenant_id, channel, external_id) values (:id, :t, 'web', :ext)",
        id=cliente, t=tid, ext=f"web:{visitante}",
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id) "
        "values (:id, :t, :c, 'web', :cc)",
        id=conversacion, t=tid, c=cliente, cc=connection_id,
    )
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content) "
        "values (:id, :t, :c, 'outbound', 'agent', :txt)",
        id=uuid.uuid4(), t=tid, c=conversacion, txt=f"respuesta para {visitante}",
    )
    return conversacion


async def _entrada_web(tid: uuid.UUID, key: str, visitante: str):
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        conversacion, _, _ = await consumer._resolve_conversation(
            {"channel": "web", "customer_external_id": f"web:{visitante}", "connection_external_id": key}
        )
    return conversacion


# ---------------------------------------------------------------- configuración
async def test_guardar_el_widget_crea_su_conexion_sin_ser_manual(app_client) -> None:
    from core.gateway.connections import list_connections, primary_connection, save_connection
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    salida = await _guardar(app_client, tid, turnstile_secret="ts-secreto")
    key = salida["key"]
    assert len(key) >= 32 and salida["turnstile_configured"] is True and "ts-secreto" not in json.dumps(salida)

    (fila,) = await _webchat(tid)
    assert (fila.external_id, fila.active, fila.is_primary, fila.meta_connection_id) == (key, True, False, None)
    assert fila.settings == {"allowed_origins": [ORIGEN]}
    assert get_secret_box().decrypt_json(fila.credentials_encrypted) == {"turnstile_secret": "ts-secreto"}
    (legacy,) = await _sql(tid, "select web_chat_config from app_settings where tenant_id = :t", t=tid)
    assert legacy.web_chat_config["key"] == key  # doble escritura hasta el contract

    # Rotar la clave, deshabilitar y borrar el secreto: la misma conexión, al día.
    salida = await _guardar(app_client, tid, enabled=False, rotate_key=True, turnstile_secret="")
    assert salida["key"] != key
    (fila,) = await _webchat(tid)
    assert (fila.external_id, fila.active, fila.credentials_encrypted) == (salida["key"], False, None)

    # No es una conexión manual: ni se lista, ni le quita la primacía a la primera de WhatsApp.
    h = await api_headers(app_client, tid)
    r = await app_client.get("/api/v1/admin/connections", headers=h)
    assert fila.id not in {i["id"] for i in r.json()["items"]}
    r = await app_client.post(f"/api/v1/admin/connections/{fila.id}/action", headers=h, json={"action": "delete"})
    assert r.status_code == 404
    async with tenant_scope(tid):
        assert all(c.id != fila.id for c in await list_connections())
        manual, creada = await save_connection(
            "Manual", {"phone_number_id": uniq("ph-man"), "app_secret": "s", "access_token": "t"}
        )
        assert creada and manual.is_primary
        assert (await primary_connection(tid)).id == manual.id
        with pytest.raises(ValueError, match="Chat web"):
            await save_connection("x", {}, type_id="webchat")


async def test_endpoint_publico_resuelve_por_la_conexion_del_negocio_correcto(app_client) -> None:
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    tid_a, tid_b = await _negocio(), await _negocio()
    key_a = (await _guardar(app_client, tid_a))["key"]
    key_b = (await _guardar(app_client, tid_b, allowed_origins=["https://otro.example"]))["key"]

    r = await app_client.get(_publico(key_a, "config"))
    assert r.status_code == 200 and r.json() == {"enabled": True, "allowed_origins": [ORIGEN], "turnstile": False}

    visitante = uniq("vis")
    r = await app_client.post(
        _publico(key_a, "messages"), headers={"Origin": ORIGEN}, json={"content": "hola", "visitor_id": visitante}
    )
    assert r.status_code == 200, r.text
    entradas = await get_redis().xrevrange(INBOUND_STREAM, count=50)
    (campos,) = [f for _, f in entradas if f.get("external_message_id") == r.json()["message_id"]]
    assert (campos["tenant_id"], campos["connection_external_id"]) == (str(tid_a), key_a)

    # El origen de A no vale para B, y una clave borrada no resuelve.
    r = await app_client.post(
        _publico(key_b, "messages"), headers={"Origin": ORIGEN}, json={"content": "hola", "visitor_id": visitante}
    )
    assert r.status_code == 403
    await _sql(tid_b, "update channel_connections set deleted_at = now() where tenant_id = :t and type_id = 'webchat'", t=tid_b)
    assert (await app_client.get(_publico(key_b, "config"))).status_code == 404  # tampoco revive por app_settings


async def test_turnstile_lo_lee_solo_el_post_y_falla_cerrado(app_client, monkeypatch) -> None:
    import core.api.routers.web_chat as web_chat

    tid = await _negocio()
    key = (await _guardar(app_client, tid, turnstile_secret="ts-ok"))["key"]
    vistos: list[str] = []

    async def _verificar(token: str, secret: str, ip: str | None) -> bool:
        vistos.append(secret)
        return token == "bueno"

    monkeypatch.setattr(web_chat, "_verify_turnstile", _verificar)
    assert (await app_client.get(_publico(key, "config"))).json()["turnstile"] is True
    cuerpo = {"content": "hola", "visitor_id": uniq("vis")}
    r = await app_client.post(_publico(key, "messages"), headers={"Origin": ORIGEN}, json=cuerpo | {"turnstile_token": "malo"})
    assert r.status_code == 403
    r = await app_client.post(_publico(key, "messages"), headers={"Origin": ORIGEN}, json=cuerpo | {"turnstile_token": "bueno"})
    assert r.status_code == 200
    assert vistos == ["ts-ok", "ts-ok"]

    # Un secreto que no se descifra (rotación de la clave maestra) falla cerrado, sin 500.
    await _sql(tid, "update channel_connections set credentials_encrypted = 'roto' where tenant_id = :t and type_id = 'webchat'", t=tid)
    r = await app_client.post(_publico(key, "messages"), headers={"Origin": ORIGEN}, json=cuerpo | {"turnstile_token": "bueno"})
    assert (r.status_code, r.json()) == (403, {"error": "turnstile_failed"})

    # El backfill (y el próximo guardado) lo vuelven a cifrar desde `app_settings`, sin romperse.
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    assert (await backfill_conexiones(dry_run=False, tenant_id=tid)).chat_web["sincronizada"] == 1
    r = await app_client.post(_publico(key, "messages"), headers={"Origin": ORIGEN}, json=cuerpo | {"turnstile_token": "bueno"})
    assert r.status_code == 200


async def test_dos_widgets_un_visitante_no_ve_la_conversacion_del_otro(app_client) -> None:
    tid = await _negocio()
    key_a = (await _guardar(app_client, tid))["key"]
    (fila_a,) = await _webchat(tid)
    # Segundo widget del mismo negocio (todavía sin API para crearlo: HU-AOS-23).
    key_b, id_b = uniq("k") + "x" * 24, uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, type_id, category, external_id, settings) "
        "values (:id, :t, 'web', 'web', 'webchat', 'comunicacion', :k, cast(:s as jsonb))",
        id=id_b, t=tid, k=key_b, s=json.dumps({"allowed_origins": [ORIGEN]}),
    )
    visitante = uniq("vis")
    await _conversacion_web(tid, visitante, connection_id=fila_a.id)

    def _get(key: str):
        return app_client.get(_publico(key, "messages"), params={"visitor_id": visitante}, headers={"Origin": ORIGEN})

    assert [m["content"] for m in (await _get(key_a)).json()["items"]] == [f"respuesta para {visitante}"]
    assert (await _get(key_b)).json()["items"] == []


async def test_sin_conexion_cae_a_app_settings_como_antes(app_client) -> None:
    tid = await _negocio()
    key = uniq("legacy") + "y" * 24
    await _config_legacy(tid, {"enabled": True, "key": key, "allowed_origins": [ORIGEN]})
    visitante = uniq("vis")
    await _conversacion_web(tid, visitante)
    r = await app_client.get(_publico(key, "messages"), params={"visitor_id": visitante}, headers={"Origin": ORIGEN})
    assert r.status_code == 200 and len(r.json()["items"]) == 1


# ---------------------------------------------------------------- backfill
async def test_backfill_convierte_el_widget_y_liga_las_conversaciones_web() -> None:
    from core.agents.backfill_agentes import backfill_agentes
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    tid = await _negocio()
    key = uniq("legacy") + "z" * 24
    await _config_legacy(tid, {"enabled": True, "key": key, "allowed_origins": [ORIGEN], "turnstile_secret": "ts-viejo"})
    abierta = await _conversacion_web(tid, uniq("vis"))

    # El backfill de agentes exige antes la conexión del widget.
    bloqueo = await backfill_agentes(dry_run=True, tenant_id=tid, modules=_modules())
    assert bloqueo.bloqueados and "chat web" in bloqueo.bloqueados[0].mensaje

    previo = await backfill_conexiones(dry_run=True, tenant_id=tid)
    assert (previo.chat_web["creada"], previo.conversaciones_web_ligadas) == (1, 1)
    assert await _webchat(tid) == []

    reporte = await backfill_conexiones(dry_run=False, tenant_id=tid)
    assert (reporte.chat_web["creada"], reporte.conversaciones_web_ligadas) == (1, 1)
    (fila,) = await _webchat(tid)
    assert (fila.external_id, fila.active) == (key, True)
    ((ligada,),) = await _sql(tid, "select connection_id from conversations where id = :c", c=abierta)
    assert ligada == fila.id

    otra = await backfill_conexiones(dry_run=False, tenant_id=tid)
    assert (otra.chat_web["ya_estaba"], otra.conversaciones_web_ligadas, otra.abiertas_sin_ligar) == (1, 0, 0)

    # Ahora sí: el widget recibe su Coordinador y su General como las demás conexiones.
    assert not (await backfill_agentes(dry_run=False, tenant_id=tid, modules=_modules())).fallo
    assert [(k, rol) for k, rol, *_ in await _instancias(tid, fila.id)] == [
        ("coordinador", "core"), ("general", "specialist"),
    ]


# ---------------------------------------------------------------- ruteo
async def test_ruteo_web_con_la_bandera_apagada_y_encendida(app_client, monkeypatch) -> None:
    tid = await _negocio()
    visitante = uniq("vis")
    vieja = await _conversacion_web(tid, visitante)  # de antes de la conexión
    key = (await _guardar(app_client, tid))["key"]
    (fila,) = await _webchat(tid)

    # Encendida: adopta la abierta sin conexión del visitante (no abre otra ni gasta cuota).
    _bandera(monkeypatch, True)
    conversacion = await _entrada_web(tid, key, visitante)
    assert (conversacion.id, conversacion.connection_id) == (vieja, fila.id)
    assert (await _entrada_web(tid, key, visitante)).id == vieja

    # Apagada: el camino de siempre, la conversación del visitante con la conexión del widget.
    _bandera(monkeypatch, False)
    otro = uniq("vis")
    conversacion = await _entrada_web(tid, key, otro)
    assert conversacion.connection_id == fila.id


# ---------------------------------------------------------------- altas posteriores
async def test_altas_posteriores_nacen_con_coordinador_y_general(app_client, monkeypatch) -> None:
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope
    from core.waweb.connections import get_or_create_connection

    tid, _cuenta, _phone, _cid = await _con_agentes()
    esperado = [("coordinador", "core"), ("general", "specialist")]

    await _guardar(app_client, tid)
    (widget,) = await _webchat(tid)
    assert [(k, rol) for k, rol, *_ in await _instancias(tid, widget.id)] == esperado

    async with tenant_scope(tid):
        manual, _ = await save_connection(
            "Manual", {"phone_number_id": uniq("ph-man"), "app_secret": "s", "access_token": "t"}
        )
    assert [(k, rol) for k, rol, *_ in await _instancias(tid, manual.id)] == esperado
    from core.gateway.connection_types import sync_connection_types

    await sync_connection_types()  # `whatsapp_waweb` llega al catálogo con el arranque del `api`
    waweb = await get_or_create_connection(tid)
    assert [(k, rol) for k, rol, *_ in await _instancias(tid, waweb.id)] == esperado


async def test_sync_phone_numbers_da_de_alta_los_numeros_nuevos_con_su_conexion(monkeypatch) -> None:
    import core.gateway.meta_phone_numbers as phone_numbers_mod

    tid, cuenta, waba_id, _phone = await _negocio_con_byok()
    nuevo = uniq("ph-sync")

    async def _lista(*_args, **_kwargs):
        return [{"id": nuevo, "display_phone_number": "+57 300 000", "verified_name": "Tienda"}]

    monkeypatch.setattr(phone_numbers_mod, "list_phone_numbers", _lista)
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await phone_numbers_mod.sync_phone_numbers(waba_id, "tok", "sec", "v21.0", cuenta, tid)
    (conexion,) = await _conexiones(tid, nuevo)
    assert (conexion.meta_connection_id, conexion.credentials_encrypted, conexion.is_primary) == (cuenta, None, False)
    ((nombre,),) = await _sql(tid, "select verified_name from phone_numbers where tenant_id = :t and phone_number_id = :p", t=tid, p=nuevo)
    assert nombre == "Tienda"


# ---------------------------------------------------------------- de punta a punta con Meta
async def test_webhook_de_meta_a_dos_numeros_responde_por_cada_numero(app_client, monkeypatch) -> None:
    """Payload real de Meta firmado, dos números de la misma cuenta, ruteo por conexión encendido:
    una conversación por número y cada respuesta sale por el número al que escribió el cliente."""
    import core.tasks.outbound_send as outbound_mod
    from core.config import get_settings
    from core.db.models import Message, utcnow
    from core.db.session import get_session
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    secreto = get_settings().whatsapp_app_secret or "ci-meta-app-secret-not-real"
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", secreto)
    _bandera(monkeypatch, True)
    tid, cuenta, waba_id, _viejo = await _negocio_con_byok()
    uno, dos = uniq("ph-uno"), uniq("ph-dos")
    await _alta_numero(tid, cuenta, uno)
    await _alta_numero(tid, cuenta, dos)
    cliente = "57300" + uuid.uuid4().hex[:7]

    def _entry(phone: str, wamid: str) -> dict[str, Any]:
        return {
            "id": waba_id,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": phone, "display_phone_number": "573000000000"},
                    "contacts": [{"wa_id": cliente, "profile": {"name": "Cliente"}}],
                    "messages": [{"from": cliente, "id": wamid, "timestamp": "1760000000", "type": "text",
                                  "text": {"body": "hola"}}],
                },
            }],
        }

    wamids = {uno: f"wamid.{uniq('a')}", dos: f"wamid.{uniq('b')}"}
    body = json.dumps({"object": "whatsapp_business_account", "entry": [_entry(p, w) for p, w in wamids.items()]}).encode()
    firma = "sha256=" + hmac.new(secreto.encode(), body, hashlib.sha256).hexdigest()
    r = await app_client.post(
        "/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": firma}
    )
    assert r.status_code == 200 and r.json()["enqueued"] == 2, r.text

    entradas = {f["external_message_id"]: f for _, f in await get_redis().xrevrange(INBOUND_STREAM, count=200)}
    conversaciones = {}
    for phone, wamid in wamids.items():
        campos = entradas[wamid]
        assert (campos["tenant_id"], campos["connection_external_id"]) == (str(tid), phone)
        async with tenant_scope(tid):
            conversacion, _, _ = await consumer._resolve_conversation(campos)
        conversaciones[phone] = conversacion
    assert conversaciones[uno].id != conversaciones[dos].id

    enviados: list[str] = []

    class _Adaptador:
        async def send_text(self, credentials: dict[str, str], to: str, text: str) -> str:
            enviados.append(credentials["phone_number_id"])
            return f"wamid.out.{uuid.uuid4().hex[:6]}"

    monkeypatch.setattr(outbound_mod, "outbound_adapter_for", lambda _provider: _Adaptador())
    monkeypatch.setattr(outbound_mod, "_enqueue_next", _sin_cadena)
    for phone in (uno, dos):
        async with tenant_scope(tid):
            async with get_session() as session:
                conv = await session.get(type(conversaciones[phone]), conversaciones[phone].id)
                conv.service_window_expires_at = utcnow().replace(year=utcnow().year + 1)
                mensaje = Message(
                    tenant_id=tid, conversation_id=conv.id, direction="outbound", sender_type="agent",
                    content="respuesta", status="queued", client_message_id=uniq("cm"),
                )
                session.add(mensaje)
                await session.flush()
                mid = mensaje.id
        assert await outbound_mod._send_one(mid, tid) is None
    assert enviados == [uno, dos]


async def _sin_cadena(*_args: Any, **_kwargs: Any) -> None:
    return None
