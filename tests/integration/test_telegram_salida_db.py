"""ADR-0199 (PR core 4) · salida de Telegram contra Postgres y Redis reales, con la Bot API simulada.

- `resolve_credentials` da el token y el `bot_id` del bot de la conversación; pausada, borrada o con el kill-switch
  apagado no da nada, y un bot nunca es el respaldo de una conversación sin conexión.
- Un id de botón largo viaja como token en `callback_data` y el webhook lo devuelve al original; el teclado se quita.
- Ritmo: un mensaje por segundo por chat y la pausa del bot tras un 429.
- Un 401 al enviar deja la conexión `revoked`; un 409, `error`; un 403 no la toca.
- La política de voz no está atada a un proveedor: decide por la capacidad `voz` del tipo de conexión.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from test_telegram_alta_db import BASE, TelegramFalso

from core.gateway.channels.base import ProviderError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch) -> dict[str, list[Any]]:
    from core.config import get_settings
    from core.gateway.telegram import webhook

    monkeypatch.setattr(get_settings(), "public_base_url", BASE)
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)
    llamadas: dict[str, list[Any]] = {"answer": [], "edit": []}

    class _Cliente:
        def __init__(self, token: str) -> None:
            pass

        async def answer_callback_query(self, callback_query_id: str) -> bool:
            llamadas["answer"].append(callback_query_id)
            return True

        async def edit_message_reply_markup(self, chat_id: Any, message_id: int) -> bool:
            llamadas["edit"].append((chat_id, message_id))
            return True

    monkeypatch.setattr(webhook, "TelegramClient", _Cliente)
    return llamadas


@pytest.fixture(scope="module")
async def conexion() -> AsyncIterator[dict[str, Any]]:
    from _habilidades import onboard_tenant

    from core.config import get_settings
    from core.gateway.telegram import alta
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"tls-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Telegram {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    settings = get_settings()
    antes = (settings.public_base_url, settings.telegram_habilitado)
    settings.public_base_url, settings.telegram_habilitado = BASE, True
    try:
        tg = TelegramFalso()
        token = tg.bot()
        async with tenant_scope(tid):
            hecha = await alta.conectar(tid, bot_token=token, transport=tg.transport)
    finally:
        settings.public_base_url, settings.telegram_habilitado = antes
    yield {
        "tid": tid,
        "connection_id": hecha.connection_id,
        "public_id": hecha.webhook_url.rsplit("/", 1)[-1],
        "bot_id": hecha.bot_id,
        "token": token,
        "secreto": tg.bots[token]["secret"],
    }
    async with tenant_scope(tid):
        await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


async def _credenciales(c: dict[str, Any], connection_id: uuid.UUID | None = None) -> tuple[str, dict[str, str]]:
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    async with tenant_scope(c["tid"]):
        return await resolve_credentials(connection_id, None, expected_tenant_id=c["tid"])


# --------------------------------------------------------------------------- credenciales
async def test_credenciales_del_bot_y_sin_respaldo(conexion, monkeypatch) -> None:
    from core.config import get_settings

    assert await _credenciales(conexion, conexion["connection_id"]) == (
        "telegram",
        {"bot_token": conexion["token"], "bot_id": conexion["bot_id"]},
    )
    monkeypatch.setattr(get_settings(), "telegram_habilitado", False)
    assert await _credenciales(conexion, conexion["connection_id"]) == ("telegram", {})
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)

    await _sql(conexion["tid"], "update channel_connections set active = false where id = :c", c=conexion["connection_id"])
    try:
        assert await _credenciales(conexion, conexion["connection_id"]) == ("telegram", {})
    finally:
        await _sql(conexion["tid"], "update channel_connections set active = true where id = :c", c=conexion["connection_id"])

    # Aunque esté marcada como principal, un bot no es el respaldo de una conversación sin conexión.
    await _sql(conexion["tid"], "update channel_connections set is_primary = true where id = :c", c=conexion["connection_id"])
    try:
        proveedor, _creds = await _credenciales(conexion, None)
        assert proveedor != "telegram"
    finally:
        await _sql(conexion["tid"], "update channel_connections set is_primary = false where id = :c", c=conexion["connection_id"])


async def test_credenciales_de_otro_bot_no_envian(conexion) -> None:
    """`external_id` es la única fuente del `bot_id`: si las credenciales guardadas dicen otro bot, no se envía."""
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    async with tenant_scope(conexion["tid"]):
        async with get_session() as session:
            fila = await session.get(ChannelConnection, conexion["connection_id"])
            original = fila.credentials_encrypted
            creds = get_secret_box().decrypt_json(original)
            fila.credentials_encrypted = get_secret_box().encrypt_json({**creds, "bot_id": "999"})
    try:
        assert await _credenciales(conexion, conexion["connection_id"]) == ("telegram", {})
    finally:
        async with tenant_scope(conexion["tid"]):
            async with get_session() as session:
                fila = await session.get(ChannelConnection, conexion["connection_id"])
                fila.credentials_encrypted = original


# --------------------------------------------------------------------------- botones con ids largos
async def test_id_largo_viaja_como_token_y_el_webhook_lo_devuelve(conexion, app_client, _ambiente) -> None:
    from core.gateway.telegram import interactivos
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    largo = "opc:" + "z" * 120
    interactivo = {"type": "button", "body": "Elige", "buttons": [{"id": largo, "title": "Plan"}, {"id": "corto", "title": "Otro"}]}
    preparado = await interactivos.preparar(conexion["tid"], conexion["bot_id"], interactivo)
    token = preparado["buttons"][0]["id"]
    assert token.startswith("t:") and len(token.encode()) <= 64
    assert preparado["buttons"][1]["id"] == "corto" and interactivo["buttons"][0]["id"] == largo, "no muta el original"

    persona = {"id": 4242, "is_bot": False, "first_name": "Ana"}
    teclado = {"inline_keyboard": [[{"text": "Plan", "callback_data": token}]]}
    cuerpo = {
        "update_id": 1,
        "callback_query": {
            "id": "cbq-largo",
            "from": persona,
            "data": token,
            "message": {"message_id": 33, "chat": {"id": 4242, "type": "private"}, "text": "Elige", "reply_markup": teclado},
        },
    }
    r = await app_client.post(
        f"/webhooks/telegram/{conexion['public_id']}",
        content=json.dumps(cuerpo).encode(),
        headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": conexion["secreto"]},
    )
    assert r.status_code == 200, r.text
    [campos] = [
        c for _i, c in await get_redis().xrevrange(INBOUND_STREAM, count=200)
        if c.get("external_message_id") == f"tg:{conexion['bot_id']}:cb:cbq-largo"
    ]
    assert campos["interactive_id"] == largo and campos["content"] == "Plan"
    assert _ambiente["answer"] == ["cbq-largo"] and _ambiente["edit"] == [(4242, 33)], "se quita el teclado"

    assert await interactivos.resolver(conexion["tid"], conexion["bot_id"], "t:vencido") == ""
    assert await interactivos.resolver(conexion["tid"], conexion["bot_id"], "corto") == "corto"


# --------------------------------------------------------------------------- ritmo
async def test_ritmo_por_chat_y_pausa_del_bot(conexion) -> None:
    from core.gateway.telegram import ritmo

    bot = f"r{uuid.uuid4().hex[:8]}"
    assert await ritmo.esperar(conexion["tid"], bot, "1") == 0.0
    assert await ritmo.esperar(conexion["tid"], bot, "1") == 1.0, "un mensaje por segundo en el mismo chat"
    assert await ritmo.esperar(conexion["tid"], bot, "2") == 0.0, "otro chat no espera"
    await ritmo.pausar(bot, 7)
    assert 0 < await ritmo.esperar(conexion["tid"], bot, "3") <= 7


# --------------------------------------------------------------------------- estado por envío
@pytest.mark.parametrize(
    ("error", "estado"),
    [
        (ProviderError("Telegram 403", provider="telegram", http_status=403, category="RECIPIENT"), "connected"),
        (ProviderError("Telegram 409", provider="telegram", http_status=409, category="CONFIG"), "error"),
        (ProviderError("Telegram 401", provider="telegram", http_status=401, category="AUTH"), "revoked"),
    ],
)
async def test_un_error_de_envio_cambia_la_conexion_solo_si_es_suyo(conexion, error, estado) -> None:
    from core.gateway.telegram import operacion
    from core.shared.tenant import tenant_scope

    await _sql(conexion["tid"], "update telegram_connections set status = 'connected' where channel_connection_id = :c", c=conexion["connection_id"])
    async with tenant_scope(conexion["tid"]):
        await operacion.marcar_por_envio(conexion["tid"], conexion["connection_id"], error)
    [(actual,)] = await _sql(
        conexion["tid"], "select status from telegram_connections where channel_connection_id = :c", c=conexion["connection_id"]
    )
    assert actual == estado


# --------------------------------------------------------------------------- voz por capacidad
async def test_la_politica_de_voz_decide_por_la_capacidad_del_tipo(conexion, monkeypatch) -> None:
    from pack_voice import policy

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(conexion["tid"]):
        async with get_session() as session:
            cliente = Customer(tenant_id=conexion["tid"], channel="telegram", external_id=str(uuid.uuid4().int % 10**9))
            session.add(cliente)
            await session.flush()
            conversacion = Conversation(
                tenant_id=conexion["tid"],
                customer_id=cliente.id,
                channel="telegram",
                connection_id=conexion["connection_id"],
            )
            session.add(conversacion)
            await session.flush()
            conversation_id = conversacion.id
        estado = await policy._estado_de_la_conversacion(conversation_id)
    assert estado == (True, False, False), "Telegram tiene voz aunque no sea WhatsApp por QR"

    # Y el sender la encola: el texto primero y después la fila `audio/tts` que sintetiza el worker.
    from core.gateway import sender
    from core.tasks import outbound_send

    encolados: list[str] = []
    monkeypatch.setattr(outbound_send.outbound_send_task, "delay", lambda message_id, tenant_id: encolados.append(message_id))
    async with tenant_scope(conexion["tid"]):
        assert await sender.send_voice_to_conversation(conversation_id, "Tu pedido sale hoy") is True
    filas = await _sql(
        conexion["tid"],
        "select media_type, media_transcription from messages where conversation_id = :c order by created_at",
        c=conversation_id,
    )
    assert [f[0] for f in filas] == [None, "audio/tts"] and filas[1][1] == "Tu pedido sale hoy"
    assert len(encolados) == 2
