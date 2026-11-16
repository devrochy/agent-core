"""ADR-0199 (PR core 2) · alta y operación de un bot de Telegram contra Postgres real, con la Bot API simulada
(`httpx.MockTransport`).

- El alta valida el token con `getMe`, guarda la conexión y su fila, registra el webhook con un secreto y lo confirma:
  queda `connected`. Repetirla es idempotente (misma conexión, mismo `public_id`, secreto nuevo).
- Token inválido (401 o 404 de `getMe`) es 422 y no guarda nada.
- Un bot, un negocio: el mismo bot en otro negocio es 409 SIN pisar su webhook; tras desconectarlo allá, se puede.
- Un webhook ajeno es 409 hasta confirmar; uno nuestro no cuenta como ajeno.
- `setWebhook` que falla: 502, la conexión queda en `error` y repetir el alta la recupera.
- Salud: webhook cambiado, error reciente o cola grande → `degraded`; token revocado → `revoked`; una caída de
  Telegram no cambia el estado. Con el kill-switch apagado el cron no consulta a Telegram.
- Cambio de token: el del mismo bot conserva el `public_id`; el de otro bot es 409.
- Desconectar quita el webhook y libera el bot.
- Rutas: kill-switch apagado → 404 (salvo desconectar); colaborador → 403; otro negocio → 404.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "https://api.solvershub.test"


def _token(bot_id: int) -> str:
    return f"{bot_id}:AAH" + secrets.token_urlsafe(24)


class TelegramFalso:
    """La Bot API para un conjunto de bots. Guarda el webhook y el secreto que se le registran."""

    def __init__(self) -> None:
        self.bots: dict[str, dict[str, Any]] = {}  # token → estado
        self.llamadas: list[str] = []
        self.falla_set_webhook = False
        self.caido = False

    def bot(self, *, webhook: str = "") -> str:
        bot_id = secrets.randbelow(10**9) + 10**9
        token = _token(bot_id)
        self.bots[token] = {
            "id": bot_id,
            "username": f"bot{bot_id}",
            "url": webhook,
            "secret": "",
            "pending": 0,
            "last_error_date": None,
            "last_error_message": None,
            "revocado": False,
        }
        return token

    def otro_token_del_mismo_bot(self, token: str) -> str:
        nuevo = f"{self.bots[token]['id']}:AAH" + secrets.token_urlsafe(24)
        self.bots[nuevo] = self.bots[token]
        return nuevo

    def handler(self, request: httpx.Request) -> httpx.Response:
        _, segmento, metodo = request.url.path.split("/", 2)
        token = segmento.removeprefix("bot")
        self.llamadas.append(metodo)
        if self.caido:
            return httpx.Response(502, json={"ok": False, "error_code": 502, "description": "Bad Gateway"})
        bot = self.bots.get(token)
        if bot is None or bot["revocado"]:
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        cuerpo = json.loads(request.content or b"{}")
        if metodo == "getMe":
            resultado: Any = {"id": bot["id"], "is_bot": True, "username": bot["username"], "first_name": "Bot"}
        elif metodo == "getWebhookInfo":
            resultado = {"url": bot["url"], "pending_update_count": bot["pending"], "has_custom_certificate": False}
            if bot["last_error_date"]:
                resultado |= {"last_error_date": bot["last_error_date"], "last_error_message": bot["last_error_message"]}
        elif metodo == "setWebhook":
            if self.falla_set_webhook:
                return httpx.Response(500, json={"ok": False, "error_code": 500, "description": "Internal"})
            bot["url"], bot["secret"] = cuerpo["url"], cuerpo["secret_token"]
            bot.setdefault("descartes", []).append(("setWebhook", cuerpo.get("drop_pending_updates")))
            resultado = True
        elif metodo == "deleteWebhook":
            bot["url"], bot["secret"] = "", ""
            bot.setdefault("descartes", []).append(("deleteWebhook", cuerpo.get("drop_pending_updates")))
            resultado = True
        elif metodo == "sendMessage":
            if cuerpo["chat_id"] == 404:
                return httpx.Response(
                    403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot can't initiate conversation"}
                )
            resultado = {"message_id": 77, "chat": {"id": cuerpo["chat_id"]}}
        else:
            return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})
        return httpx.Response(200, json={"ok": True, "result": resultado})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "public_base_url", BASE)
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("tla", "tlb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Telegram {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
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


async def _conectar(tid: uuid.UUID, tg: TelegramFalso, token: str, **extra: Any):
    from core.gateway.telegram import alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await alta.conectar(tid, bot_token=token, transport=tg.transport, **extra)


async def _op(tid: uuid.UUID, nombre: str, *args: Any, **kw: Any) -> Any:
    from core.gateway.telegram import operacion
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await getattr(operacion, nombre)(tid, *args, **kw)


async def _error(coro) -> Any:
    from core.gateway.telegram.alta import AltaError

    with pytest.raises(AltaError) as exc:
        await coro
    return exc.value


async def _secreto(tid: uuid.UUID, connection_id: uuid.UUID) -> str:
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.gateway.connections import credentials_of
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return str(credentials_of(await session.get(ChannelConnection, connection_id))["webhook_secret"])


# --------------------------------------------------------------------------- alta
async def test_alta_conecta_y_es_idempotente(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot()
    hecha = await _conectar(tid, tg, token)

    assert hecha.created is True and hecha.status == "connected"
    assert hecha.webhook_confirmado_at is not None
    bot = tg.bots[token]
    assert hecha.bot_id == str(bot["id"]) and hecha.bot_username == bot["username"]
    assert bot["url"] == hecha.webhook_url and hecha.webhook_url.startswith(f"{BASE}/webhooks/telegram/")
    assert bot["secret"] == await _secreto(tid, hecha.connection_id)
    [(external_id, provider, channel, type_id, cifradas)] = await _sql(
        tid,
        "select external_id, provider, channel, type_id, credentials_encrypted from channel_connections where id = :c",
        c=hecha.connection_id,
    )
    assert (external_id, provider, channel, type_id) == (str(bot["id"]), "telegram", "telegram", "telegram")
    assert token not in cifradas

    primer_secreto = bot["secret"]
    otra_vez = await _conectar(tid, tg, token)
    assert otra_vez.created is False and otra_vez.connection_id == hecha.connection_id
    assert otra_vez.webhook_url == hecha.webhook_url, "mismo public_id"
    assert bot["secret"] != primer_secreto, "cada alta rota el secreto del webhook"
    # Repetir el alta sobre la misma conexión conserva lo pendiente: es de este negocio.
    assert bot["descartes"] == [("setWebhook", True), ("setWebhook", False)]


@pytest.mark.parametrize("token", ["no-es-un-token", "123456789:" + "x" * 35])
async def test_token_invalido_es_422_y_no_guarda(negocios, token) -> None:
    tid, _ = negocios
    tg = TelegramFalso()  # sin bots: getMe responde 401
    error = await _error(_conectar(tid, tg, token))
    assert (error.http_status, error.code) == (422, "telegram_token_invalido")
    assert "setWebhook" not in tg.llamadas


async def test_un_bot_en_un_solo_negocio_sin_pisar_su_webhook(negocios) -> None:
    a, b = negocios
    tg = TelegramFalso()
    token = tg.bot()
    hecha = await _conectar(a, tg, token)
    url_de_a = tg.bots[token]["url"]

    error = await _error(_conectar(b, tg, token))
    assert (error.http_status, error.code) == (409, "bot_en_otro_negocio")
    assert tg.bots[token]["url"] == url_de_a, "el webhook del otro negocio no se tocó"

    assert await _op(a, "desconectar", hecha.connection_id, transport=tg.transport) is True
    assert tg.bots[token]["url"] == ""
    de_b = await _conectar(b, tg, token)
    assert de_b.status == "connected" and de_b.connection_id != hecha.connection_id
    # Lo que Telegram tenía pendiente para A se descarta al desconectar y al conectar en B: nunca le llega a B.
    assert tg.bots[token]["descartes"] == [("setWebhook", True), ("deleteWebhook", True), ("setWebhook", True)]


async def test_webhook_ajeno_pide_confirmacion(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot(webhook="https://otra-integracion.example/hook")
    error = await _error(_conectar(tid, tg, token))
    assert (error.http_status, error.code) == (409, "webhook_ajeno")
    assert error.details == {"webhook_url": "https://otra-integracion.example/hook"}
    assert await _sql(tid, "select 1 from channel_connections where external_id = :b", b=str(tg.bots[token]["id"])) == []

    hecha = await _conectar(tid, tg, token, confirm_overwrite_webhook=True)
    assert hecha.status == "connected" and tg.bots[token]["url"] == hecha.webhook_url


async def test_set_webhook_que_falla_deja_error_y_se_recupera(negocios) -> None:
    from core.gateway.telegram import alta
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot()
    tg.falla_set_webhook = True
    error = await _error(_conectar(tid, tg, token))
    assert (error.http_status, error.code) == (502, "telegram_webhook_no_registrado")
    connection_id = uuid.UUID(error.details["connection_id"])
    async with tenant_scope(tid):
        guardada = await alta.obtener(tid, connection_id)
    assert guardada is not None and guardada.status == "error" and guardada.last_error_message

    tg.falla_set_webhook = False
    recuperada = await _conectar(tid, tg, token)
    assert recuperada.connection_id == connection_id and recuperada.status == "connected"
    assert recuperada.last_error_message is None


async def test_sin_url_publica_https_no_toca_telegram(negocios, monkeypatch) -> None:
    from core.config import get_settings

    tid, _ = negocios
    monkeypatch.setattr(get_settings(), "public_base_url", "http://localhost:8000")
    tg = TelegramFalso()
    error = await _error(_conectar(tid, tg, tg.bot()))
    assert (error.http_status, error.code) == (503, "url_publica_no_https")
    assert tg.llamadas == []


async def test_pausada_no_se_reactiva_con_el_alta(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot()
    hecha = await _conectar(tid, tg, token)
    await _sql(tid, "update channel_connections set active = false where id = :c", c=hecha.connection_id)
    error = await _error(_conectar(tid, tg, token))
    assert (error.http_status, error.code) == (409, "conexion_pausada")


# --------------------------------------------------------------------------- salud
async def test_salud(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot()
    hecha = await _conectar(tid, tg, token)
    bot = tg.bots[token]

    async def estado() -> str:
        r = await _op(tid, "revisar", hecha.connection_id, transport=tg.transport)
        return r.status

    assert await estado() == "connected"
    bot["pending"] = 51
    assert await estado() == "degraded"
    bot["pending"] = 0
    bot["last_error_date"], bot["last_error_message"] = int(time.time()) - 60, "Wrong response from the webhook: 404"
    assert await estado() == "degraded"
    bot["last_error_date"] = int(time.time()) - 3600
    assert await estado() == "connected", "un error viejo no degrada"
    bot["url"] = "https://otra.example/hook"
    assert await estado() == "degraded"

    reparada = await _op(tid, "reparar_webhook", hecha.connection_id, transport=tg.transport)
    assert reparada.status == "connected" and bot["url"] == hecha.webhook_url
    assert bot["secret"] == await _secreto(tid, hecha.connection_id), "reparar usa el secreto guardado"

    tg.caido = True
    assert await estado() == "connected", "una caída de Telegram no cambia el estado"
    tg.caido = False
    bot["revocado"] = True
    assert await estado() == "revoked"


async def test_el_cron_no_consulta_con_el_kill_switch_apagado(negocios, monkeypatch) -> None:
    from core.config import get_settings
    from core.gateway.telegram import operacion

    monkeypatch.setattr(get_settings(), "telegram_habilitado", False)
    assert await operacion.revisar_todas() == 0


async def test_admin_ids_ve_las_conexiones_de_todos(negocios) -> None:
    a, b = negocios
    tg = TelegramFalso()
    ca = await _conectar(a, tg, tg.bot())
    cb = await _conectar(b, tg, tg.bot())
    ids = {r[0] for r in await _sql(None, "select id from admin_telegram_connection_ids()")}
    assert {ca.connection_id, cb.connection_id} <= ids


# --------------------------------------------------------------------------- operación
async def test_cambio_de_token(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot()
    hecha = await _conectar(tid, tg, token)
    nuevo = tg.otro_token_del_mismo_bot(token)
    tg.bots[token] = dict(tg.bots[token], revocado=True)

    rotada = await _op(tid, "rotar_credenciales", hecha.connection_id, bot_token=nuevo, transport=tg.transport)
    assert rotada.connection_id == hecha.connection_id and rotada.webhook_url == hecha.webhook_url
    assert rotada.status == "connected"

    error = await _error(_op(tid, "rotar_credenciales", hecha.connection_id, bot_token=tg.bot(), transport=tg.transport))
    assert (error.http_status, error.code) == (409, "otro_bot")


async def test_mensaje_de_prueba(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    hecha = await _conectar(tid, tg, tg.bot())
    assert await _op(tid, "enviar_prueba", hecha.connection_id, 12345, transport=tg.transport) == 77
    otra = await _conectar(tid, tg, tg.bot())
    error = await _error(_op(tid, "enviar_prueba", otra.connection_id, 404, transport=tg.transport))
    assert (error.http_status, error.code) == (409, "chat_no_iniciado")


async def test_desconectar_libera_y_borra_las_dos_filas(negocios) -> None:
    tid, _ = negocios
    tg = TelegramFalso()
    token = tg.bot()
    hecha = await _conectar(tid, tg, token)
    tg.caido = True
    error = await _error(_op(tid, "desconectar", hecha.connection_id, transport=tg.transport))
    assert (error.http_status, error.code) == (502, "webhook_no_eliminado")
    assert await _sql(tid, "select 1 from telegram_connections where channel_connection_id = :c and deleted_at is null", c=hecha.connection_id)

    assert await _op(tid, "desconectar", hecha.connection_id, continuar_si_falla=True, transport=tg.transport) is False
    assert await _sql(tid, "select 1 from channel_connections where id = :c and deleted_at is null", c=hecha.connection_id) == []
    assert await _sql(tid, "select 1 from telegram_connections where channel_connection_id = :c and deleted_at is null", c=hecha.connection_id) == []


# --------------------------------------------------------------------------- rutas
async def test_rutas_kill_switch_permisos_y_otro_negocio(negocios, app_client, monkeypatch) -> None:
    from test_opciones_api import _headers

    from core.config import get_settings

    a, b = negocios
    tg = TelegramFalso()
    hecha = await _conectar(a, tg, tg.bot())
    url = f"/api/v1/admin/connections/telegram/{hecha.connection_id}"

    h_a = await api_headers(app_client, a)
    r = await app_client.get(url, headers=h_a)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["bot_username"] == hecha.bot_username and cuerpo["webhook_url"] == hecha.webhook_url
    assert "bot_token" not in cuerpo and "webhook_secret" not in cuerpo
    r = await app_client.get("/api/v1/admin/connections/telegram", headers=h_a)
    assert r.status_code == 200 and hecha.bot_id in {c["bot_id"] for c in r.json()}

    # Las listas de conexiones (APK y panel) muestran el @usuario del bot, no su id.
    usuario = f"@{hecha.bot_username}"
    lista = (await app_client.get("/api/v1/admin/connections", headers=h_a)).json()["items"]
    assert any(i["id"] == str(hecha.connection_id) and i["display_name"] == usuario for i in lista)
    consola = (await app_client.get("/api/v1/admin/connections/console", headers=h_a)).json()["items"]
    assert any(i["id"] == str(hecha.connection_id) and i["display_name"] == usuario for i in consola)

    h_b = await api_headers(app_client, b)
    assert (await app_client.get(url, headers=h_b)).status_code == 404

    h_colab = await _headers(app_client, a, rol="colaborador", secciones={"connections": "write"})
    r = await app_client.post(f"{url}/refresh", headers=h_colab)
    assert r.status_code == 403, r.text
    r = await app_client.get(url, headers=await _headers(app_client, a, rol="colaborador", secciones={"connections": "read"}))
    assert r.status_code == 200 and r.json()["webhook_url"] == "", "el public_id solo lo ve quien administra"
    r = await app_client.post(f"/api/v1/admin/connections/{hecha.connection_id}/action", headers=h_colab, json={"action": "deactivate"})
    assert r.status_code == 403, r.text

    # El borrado genérico no quita el webhook: un bot se desconecta por su ruta.
    r = await app_client.post(
        f"/api/v1/admin/connections/{hecha.connection_id}/action", headers=h_a, json={"action": "delete"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "usar_desconectar_telegram", r.text

    monkeypatch.setattr(get_settings(), "telegram_habilitado", False)
    assert (await app_client.get(url, headers=h_a)).status_code == 404
    r = await app_client.post("/api/v1/admin/connections/telegram", headers=h_a, json={"bot_token": "1" * 25})
    assert r.status_code == 404
    catalogo = (await app_client.get("/api/v1/admin/connections/catalog", headers=h_a)).json()
    assert all(i["id"] != "telegram" or not i["available"] for c in catalogo["categories"] for i in c["items"])


async def test_el_formulario_manual_no_convierte_un_bot(negocios) -> None:
    from core.db.session import get_session
    from core.gateway.connections import save_connection_in
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    tg = TelegramFalso()
    hecha = await _conectar(tid, tg, tg.bot())
    async with tenant_scope(tid):
        async with get_session() as session:
            with pytest.raises(ValueError):
                await save_connection_in(
                    session,
                    "x",
                    {"host": "smtp.x", "port": 587, "username": "u", "password": "p", "from_address": "a@x.co"},
                    connection_id=hecha.connection_id,
                    type_id="email_smtp",
                )
