"""ADR-0199 (PR core 3) · webhook de Telegram de punta a punta, contra Postgres y Redis reales y con la Bot API
simulada.

- Un mensaje con el secreto correcto se publica una vez en el stream con la conexión validada y los ids globales;
  el reintento de Telegram (mismo `update_id`) no lo duplica. La auditoría no guarda el texto.
- Secreto malo o ausente: 401. `public_id` desconocido: 404. Cuerpo de más de 64 KiB: 413. Kill-switch apagado: 503.
- Grupo, mensaje editado o JSON inválido: 200 sin publicar. Conexión pausada: 200 sin publicar.
- Un toque de botón se publica con su `interactive_id` y se contesta con `answerCallbackQuery`.
- El consumer arma el cliente de Telegram sin teléfono y no crea conversación sin la conexión validada.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

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


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch) -> list[str]:
    """URL pública https, kill-switch encendido y `answerCallbackQuery` simulado (el webhook no recibe transporte)."""
    from core.config import get_settings
    from core.gateway.telegram import webhook

    monkeypatch.setattr(get_settings(), "public_base_url", BASE)
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)
    contestados: list[str] = []

    class _Cliente:
        def __init__(self, token: str) -> None:
            assert token, "el webhook contesta con el token de la conexión"

        async def answer_callback_query(self, callback_query_id: str) -> bool:
            contestados.append(callback_query_id)
            return True

        async def edit_message_reply_markup(self, chat_id: Any, message_id: int) -> bool:
            return True

    monkeypatch.setattr(webhook, "TelegramClient", _Cliente)
    return contestados


@pytest.fixture(scope="module")
async def conexion() -> AsyncIterator[dict[str, Any]]:
    async for c in _negocio_con_bot("tlw"):
        yield c


@pytest.fixture(scope="module")
async def otra_conexion() -> AsyncIterator[dict[str, Any]]:
    async for c in _negocio_con_bot("tlx"):
        yield c


async def _negocio_con_bot(prefijo: str) -> AsyncIterator[dict[str, Any]]:
    from _habilidades import onboard_tenant

    from core.config import get_settings
    from core.gateway.telegram import alta
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
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
        "secreto": tg.bots[token]["secret"],
    }
    async with tenant_scope(tid):
        await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


def _update_id() -> int:
    return secrets.randbelow(10**9) + 1


def _mensaje(update_id: int, texto: str = "Hola", **extra: Any) -> dict[str, Any]:
    """Un update con un mensaje cuyo `message_id` es el mismo número: así cada prueba usa un id propio."""
    persona = {"id": 4242, "is_bot": False, "first_name": "Ana"}
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "from": persona, "chat": {"id": 4242, "type": "private"}, "text": texto, **extra},
    }


async def _post(app_client, c: dict[str, Any], cuerpo: Any, *, secreto: str | None = None, public_id: str | None = None):
    headers = {"Content-Type": "application/json"}
    if secreto != "":
        headers["X-Telegram-Bot-Api-Secret-Token"] = c["secreto"] if secreto is None else secreto
    contenido = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
    return await app_client.post(f"/webhooks/telegram/{public_id or c['public_id']}", content=contenido, headers=headers)


def _id(c: dict[str, Any], uid: int) -> str:
    return f"tg:{c['bot_id']}:in:4242:{uid}"


async def _encolados(external_id: str) -> list[dict[str, str]]:
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    filas = await get_redis().xrevrange(INBOUND_STREAM, count=500)
    return [campos for _id, campos in filas if campos.get("external_message_id") == external_id]


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


async def test_mensaje_se_publica_una_vez_con_ids_globales(conexion, app_client) -> None:
    uid = _update_id()
    cuerpo = _mensaje(uid, "Quiero saber el precio")
    r = await _post(app_client, conexion, cuerpo)
    assert r.status_code == 200, r.text
    assert (await _post(app_client, conexion, cuerpo)).status_code == 200  # reintento de Telegram

    [campos] = await _encolados(f"tg:{conexion['bot_id']}:in:4242:{uid}")
    external_id = f"tg:{conexion['bot_id']}:{uid}"
    assert campos["provider"] == "telegram" and campos["channel"] == "telegram"
    assert campos["customer_external_id"] == "4242" and campos["customer_name"] == "Ana"
    assert campos["connection_external_id"] == conexion["bot_id"]
    assert campos["connection_id"] == str(conexion["connection_id"])
    assert campos["tenant_id"] == str(conexion["tid"])

    [(payload, app_id)] = await _sql(
        None,
        "select payload, meta_app_id from webhook_events where external_id = :e and tenant_id = :t",
        e=external_id,
        t=conexion["tid"],
    )
    assert app_id == f"telegram:{conexion['bot_id']}"
    assert "Quiero saber" not in json.dumps(payload), "la auditoría no guarda el texto"
    [(ultimo,)] = await _sql(
        conexion["tid"],
        "select last_update_at from telegram_connections where channel_connection_id = :c",
        c=conexion["connection_id"],
    )
    assert ultimo is not None


@pytest.mark.parametrize("secreto", ["otro-secreto-" + "x" * 30, "", "corto", "con espacios " + "x" * 40])
async def test_secreto_malo_es_401_no_publica_ni_audita(conexion, app_client, secreto) -> None:
    uid = _update_id()
    r = await _post(app_client, conexion, _mensaje(uid), secreto=secreto)
    assert r.status_code == 401
    assert await _encolados(_id(conexion, uid)) == []
    assert await _sql(
        None, "select 1 from webhook_events where external_id = :e", e=f"tg:{conexion['bot_id']}:{uid}"
    ) == []


async def test_el_secreto_de_otro_negocio_no_abre_esta_conexion(conexion, otra_conexion, app_client) -> None:
    """Con el `public_id` de A y el secreto de B (otro negocio, otro bot): 401, nada publicado ni auditado."""
    uid = _update_id()
    r = await _post(app_client, conexion, _mensaje(uid), secreto=otra_conexion["secreto"])
    assert r.status_code == 401
    assert await _encolados(_id(conexion, uid)) == [] and await _encolados(_id(otra_conexion, uid)) == []
    r = await _post(app_client, otra_conexion, _mensaje(uid))
    assert r.status_code == 200
    [campos] = await _encolados(_id(otra_conexion, uid))
    assert campos["tenant_id"] == str(otra_conexion["tid"]) != str(conexion["tid"])


async def test_public_id_desconocido_cuerpo_grande_y_kill_switch(conexion, app_client, monkeypatch) -> None:
    from core.config import get_settings

    assert (await _post(app_client, conexion, _mensaje(_update_id()), public_id="no-existe-" + "x" * 12)).status_code == 404
    grande = json.dumps(_mensaje(_update_id(), "x" * (70 * 1024))).encode()
    assert (await _post(app_client, conexion, grande)).status_code == 413
    monkeypatch.setattr(get_settings(), "telegram_habilitado", False)
    uid = _update_id()
    assert (await _post(app_client, conexion, _mensaje(uid))).status_code == 503
    assert await _encolados(_id(conexion, uid)) == []


@pytest.mark.parametrize(
    "cuerpo",
    [
        lambda uid: _mensaje(uid, chat={"id": -100, "type": "group"}),
        lambda uid: {"update_id": uid, "edited_message": _mensaje(uid)["message"]},
        lambda uid: {"update_id": uid, "message": {**_mensaje(uid)["message"], "text": None, "sticker": {"file_id": "s"}}},
    ],
)
async def test_lo_no_soportado_es_200_sin_publicar(conexion, app_client, cuerpo) -> None:
    uid = _update_id()
    r = await _post(app_client, conexion, cuerpo(uid))
    assert r.status_code == 200
    assert await _encolados(_id(conexion, uid)) == []


async def test_json_invalido_es_200(conexion, app_client) -> None:
    assert (await _post(app_client, conexion, b"{no es json")).status_code == 200


async def test_conexion_pausada_no_publica(conexion, app_client) -> None:
    await _sql(conexion["tid"], "update channel_connections set active = false where id = :c", c=conexion["connection_id"])
    try:
        uid = _update_id()
        assert (await _post(app_client, conexion, _mensaje(uid))).status_code == 200
        assert await _encolados(_id(conexion, uid)) == []
    finally:
        await _sql(conexion["tid"], "update channel_connections set active = true where id = :c", c=conexion["connection_id"])


async def test_toque_de_boton_se_publica_y_se_contesta(conexion, app_client, _ambiente) -> None:
    uid = _update_id()
    persona = {"id": 4242, "is_bot": False, "first_name": "Ana"}
    teclado = {"inline_keyboard": [[{"text": "Sí, confirmar", "callback_data": "confirmar:si"}]]}
    cuerpo = {
        "update_id": uid,
        "callback_query": {
            "id": "cbq-77",
            "from": persona,
            "data": "confirmar:si",
            "message": {"message_id": 9, "chat": {"id": 4242, "type": "private"}, "text": "¿Confirmas?", "reply_markup": teclado},
        },
    }
    assert (await _post(app_client, conexion, cuerpo)).status_code == 200
    [campos] = await _encolados(f"tg:{conexion['bot_id']}:cb:cbq-77")
    assert campos["interactive_id"] == "confirmar:si" and campos["content"] == "Sí, confirmar"
    assert _ambiente == ["cbq-77"]


async def test_el_consumer_arma_el_cliente_de_telegram_y_falla_cerrado_sin_conexion(conexion) -> None:
    from core.orchestrator.consumer import InboundSinConexion, _resolve_conversation
    from core.shared.tenant import tenant_scope

    base = {
        "provider": "telegram",
        "channel": "telegram",
        "customer_external_id": f"{secrets.randbelow(10**9)}",
        "customer_name": "Luis",
        "connection_external_id": conexion["bot_id"],
    }
    async with tenant_scope(conexion["tid"]):
        conversacion, cliente, _ = await _resolve_conversation({**base, "connection_id": str(conexion["connection_id"])})
        assert cliente.channel == "telegram" and cliente.external_id == base["customer_external_id"]
        assert cliente.contact_phone is None
        assert conversacion.connection_id == conexion["connection_id"]
        with pytest.raises(InboundSinConexion):
            await _resolve_conversation({**base, "customer_external_id": "1", "connection_id": str(uuid.uuid4())})
