"""HU-CONN-26 «Pedir» (ADR-0199 §10) de punta a punta contra Postgres y Redis reales, con la Bot API simulada.

La tool `pedir_telefono` encola un `contacto` (una sola vez por llamada), el adaptador lo manda como teclado de
respuesta con `request_contact` y, cuando el cliente comparte su contacto, el webhook y el consumer dejan el número en
`contact_phone`. Después la tool ya no lo pide. En una conversación de otro canal no se encola nada, y otro negocio no
ve el mensaje encolado.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from test_telegram_alta_db import BASE, TelegramFalso

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

ARGS = {"texto": "Para encontrar tu cuenta necesito tu número.", "etiqueta": "Compartir mi teléfono"}


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch) -> list[tuple[str, ...]]:
    from core.config import get_settings
    from core.tasks.outbound_send import outbound_send_task

    monkeypatch.setattr(get_settings(), "public_base_url", BASE)
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)
    encolados: list[tuple[str, ...]] = []
    monkeypatch.setattr(outbound_send_task, "delay", lambda *a: encolados.append(a))
    return encolados


@pytest.fixture(scope="module")
async def conexion() -> AsyncIterator[dict[str, Any]]:
    from _habilidades import onboard_tenant

    from core.config import get_settings
    from core.gateway.telegram import alta
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"tlp-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(plan="empresarial", name=f"Telegram {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
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


async def _resolver(c: dict[str, Any], campos: dict[str, str]) -> Any:
    from core.orchestrator.consumer import _resolve_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(c["tid"]):
        return await _resolve_conversation(campos)


async def _pedir(c: dict[str, Any], conversacion: Any, cliente: Any, clave: str, channel: str = "telegram") -> dict:
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope
    from core.tools.core_skill import CoreSkill

    ctx = ToolContext(
        conversation_id=conversacion.id,
        customer_id=cliente.id,
        idempotency_key=f"{conversacion.id}:{clave}",
        channel=channel,
    )
    async with tenant_scope(c["tid"]):
        return await CoreSkill()._pedir_telefono(dict(ARGS), ctx)


async def test_pedir_compartir_y_queda_en_contact_phone(conexion, app_client, _ambiente) -> None:
    from agentcore_channel_telegram.adapters import TelegramOutbound

    from core.gateway.telegram import interactivos
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    usuario = 10**9 + secrets.randbelow(10**9)
    campos = {
        "provider": "telegram",
        "channel": "telegram",
        "customer_external_id": str(usuario),
        "customer_name": "Ana",
        "connection_external_id": conexion["bot_id"],
        "connection_id": str(conexion["connection_id"]),
    }
    conversacion, cliente, _ = await _resolver(conexion, campos)

    # 1. La tool encola el pedido una sola vez por llamada.
    assert await _pedir(conexion, conversacion, cliente, "call_1") == {"status": "enviado"}
    assert await _pedir(conexion, conversacion, cliente, "call_1") == {"status": "ya_enviado"}
    [(mensaje_id, interactivo, estado)] = await _sql(
        conexion["tid"],
        "select id, interactive, status from messages where client_message_id = :k",
        k=f"pedir_tel:{conversacion.id}:call_1",
    )
    assert estado == "queued" and interactivo == {"type": "contacto", "body": ARGS["texto"], "button": ARGS["etiqueta"]}
    assert [a[0] for a in _ambiente] == [str(mensaje_id)], "un solo envío a la cola"
    # Otro negocio no ve el mensaje encolado (RLS).
    assert await _sql(uuid.uuid4(), "select 1 from messages where id = :i", i=mensaje_id) == []

    # 2. El adaptador lo manda como teclado de respuesta con `request_contact` (lo que hace `_send_one`).
    enviados: list[dict[str, Any]] = []

    def bot_api(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 70, "chat": {"id": usuario}}})

    preparado = await interactivos.preparar(conexion["tid"], conexion["bot_id"], interactivo)
    creds = {"bot_token": conexion["token"], "bot_id": conexion["bot_id"]}
    await TelegramOutbound(httpx.MockTransport(bot_api)).send_interactive(creds, str(usuario), preparado)
    [cuerpo] = enviados
    assert cuerpo["chat_id"] == usuario and cuerpo["reply_markup"]["keyboard"] == [
        [{"text": ARGS["etiqueta"], "request_contact": True}]
    ]
    assert cuerpo["reply_markup"]["one_time_keyboard"] is True

    # 3. El cliente toca el botón: llega su propio contacto (Telegram a veces sin `+`).
    persona = {"id": usuario, "is_bot": False, "first_name": "Ana"}
    update = {
        "update_id": 9,
        "message": {
            "message_id": 71,
            "from": persona,
            "chat": {"id": usuario, "type": "private"},
            "contact": {"phone_number": "573001234567", "first_name": "Ana", "user_id": usuario},
        },
    }
    r = await app_client.post(
        f"/webhooks/telegram/{conexion['public_id']}",
        content=json.dumps(update).encode(),
        headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": conexion["secreto"]},
    )
    assert r.status_code == 200, r.text
    [entrante] = [
        c for _i, c in await get_redis().xrevrange(INBOUND_STREAM, count=200)
        if c.get("external_message_id") == f"tg:{conexion['bot_id']}:in:{usuario}:71"
    ]
    assert "573001234567" not in entrante["content"], "el número no va en el texto del turno"
    await _resolver(conexion, entrante)
    [(telefono,)] = await _sql(conexion["tid"], "select contact_phone from customers where id = :i", i=cliente.id)
    assert telefono == "+573001234567"

    # 4. Ya no se vuelve a pedir, y la respuesta no lleva el número.
    assert await _pedir(conexion, conversacion, cliente, "call_2") == {"status": "ya_tiene_telefono"}


async def test_en_otro_canal_no_se_encola_nada(conexion, _ambiente) -> None:
    usuario = str(10**9 + secrets.randbelow(10**9))
    campos = {
        "provider": "telegram",
        "channel": "telegram",
        "customer_external_id": usuario,
        "customer_name": "Luis",
        "connection_external_id": conexion["bot_id"],
        "connection_id": str(conexion["connection_id"]),
    }
    conversacion, cliente, _ = await _resolver(conexion, campos)
    resultado = await _pedir(conexion, conversacion, cliente, "call_wa", channel="whatsapp")
    assert resultado["error"] == "canal_sin_pedir_contacto"
    assert await _sql(
        conexion["tid"], "select 1 from messages where client_message_id = :k", k=f"pedir_tel:{conversacion.id}:call_wa"
    ) == []
    assert _ambiente == []
