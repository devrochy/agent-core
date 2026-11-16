"""Canal Telegram, salida (ADR-0199, PR core 4): `TelegramOutbound` y su paso por `outbound_send._send_one`.

Sin red ni base: la Bot API es un `httpx.MockTransport` y `_send_one` corre con el fixture de
`test_outbound_send_task.py`. Lo que necesita Postgres y Redis está en `tests/integration/test_telegram_salida_db.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agentcore_channel_telegram import OutboundAdapter
from agentcore_channel_telegram.adapters import TelegramOutbound, teclado_inline
from test_outbound_send_task import _send_one_fixture

from core.gateway.channels.base import ProviderError
from core.tasks.outbound_send import decide_retry

TOKEN = "123456789:AAH" + "x" * 32
CREDS = {"bot_token": TOKEN, "bot_id": "123456789"}


class BotFalso:
    """Bot API que guarda lo que se le manda. `respuesta` fija el error del próximo envío."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, Any]] = []
        self.respuesta: httpx.Response | None = None
        self._siguiente = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        metodo = request.url.path.rsplit("/", 1)[-1]
        cuerpo = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else {}
        self.enviados.append((metodo, cuerpo))
        if self.respuesta is not None:
            r, self.respuesta = self.respuesta, None
            return r
        self._siguiente += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self._siguiente, "chat": {"id": 555}}})

    @property
    def adaptador(self) -> TelegramOutbound:
        return TelegramOutbound(httpx.MockTransport(self.handler))


def _error(status: int, descripcion: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "error_code": status, "description": descripcion, **extra})


def test_el_paquete_expone_el_adaptador_de_salida():
    assert OutboundAdapter is TelegramOutbound


# ------------------------------------------------------------------ adaptador
async def test_texto_devuelve_el_id_global():
    bot = BotFalso()
    assert await bot.adaptador.send_text(CREDS, "555", "Hola") == "tg:123456789:out:555:101"
    assert bot.enviados == [("sendMessage", {"chat_id": 555, "text": "Hola"})]


async def test_texto_largo_sale_en_partes_en_orden():
    bot = BotFalso()
    texto = "a" * 4096 + "b" * 10
    assert await bot.adaptador.send_text(CREDS, "555", texto) == "tg:123456789:out:555:102"
    assert [c["text"] for _m, c in bot.enviados] == ["a" * 4096, "b" * 10]


async def test_destinatario_que_no_es_un_id_es_recipient():
    with pytest.raises(ProviderError) as exc:
        await BotFalso().adaptador.send_text(CREDS, "whatsapp:+57300", "Hola")
    assert exc.value.category == "RECIPIENT"


async def test_foto_y_voz_por_url():
    bot = BotFalso()
    await bot.adaptador.send_image(CREDS, "555", "https://x/foto.jpg", "c" * 2000)
    await bot.adaptador.send_audio(CREDS, "555", "https://x/voz.ogg")
    (m1, c1), (m2, c2) = bot.enviados
    assert m1 == "sendPhoto" and c1["photo"] == "https://x/foto.jpg" and len(c1["caption"]) == 1024
    assert m2 == "sendVoice" and c2 == {"chat_id": 555, "voice": "https://x/voz.ogg"}


async def test_botones_y_lista_van_como_teclado_inline_una_fila_por_opcion():
    bot = BotFalso()
    botones = {"type": "button", "body": "¿Confirmas?", "buttons": [{"id": "si", "title": "Sí"}, {"id": "no", "title": "No"}]}
    await bot.adaptador.send_interactive(CREDS, "555", botones)
    lista = {
        "type": "list",
        "body": "Elige",
        "button": "Ver",
        "rows": [{"id": f"r{i}", "title": f"Opción {i}"} for i in range(10)],
    }
    await bot.adaptador.send_interactive(CREDS, "555", lista)
    (_, c1), (_, c2) = bot.enviados
    assert c1["text"] == "¿Confirmas?"
    assert c1["reply_markup"] == {"inline_keyboard": [[{"text": "Sí", "callback_data": "si"}], [{"text": "No", "callback_data": "no"}]]}
    assert len(c2["reply_markup"]["inline_keyboard"]) == 10, "la lista no se degrada: una fila por opción"


def test_un_id_que_no_cabe_es_error_de_contenido():
    with pytest.raises(ProviderError) as exc:
        teclado_inline({"type": "button", "buttons": [{"id": "x" * 65, "title": "Largo"}]})
    assert exc.value.category == "CONTENT"


async def test_ok_sin_message_id_es_ambiguo():
    bot = BotFalso()
    bot.respuesta = httpx.Response(200, json={"ok": True, "result": {}})
    with pytest.raises(ProviderError) as exc:
        await bot.adaptador.send_text(CREDS, "555", "Hola")
    assert exc.value.category == "AMBIGUOUS"


# ------------------------------------------------------------------ _send_one
def _con_telegram(monkeypatch, bot: BotFalso, *, espera: float = 0.0, creds: dict[str, str] | None = None, **kw: Any):
    import core.tasks.outbound_send as mod
    from core.gateway.telegram import interactivos, operacion, ritmo

    msg, tenant_id, calls = _send_one_fixture(monkeypatch, adapter=bot.adaptador, channel="telegram", **kw)
    calls.update(marcados=[], pausas=[])

    async def credenciales(*a, **k):
        return ("telegram", CREDS if creds is None else creds)

    async def esperar(*a, **k):
        return espera

    async def pausar(bot_id, segundos):
        calls["pausas"].append((bot_id, segundos))

    async def marcar(tenant_id, connection_id, exc):
        calls["marcados"].append(exc.category)

    async def preparar(tenant_id, bot_id, interactive):
        return interactive

    monkeypatch.setattr(mod, "resolve_credentials", credenciales)
    monkeypatch.setattr(ritmo, "esperar", esperar)
    monkeypatch.setattr(ritmo, "pausar", pausar)
    monkeypatch.setattr(operacion, "marcar_por_envio", marcar)
    monkeypatch.setattr(interactivos, "preparar", preparar)
    return msg, tenant_id, calls


async def test_send_one_envia_texto_y_guarda_el_id(monkeypatch):
    from core.tasks.outbound_send import _send_one

    bot = BotFalso()
    msg, tenant_id, _calls = _con_telegram(monkeypatch, bot)
    _patch_cliente(monkeypatch)
    assert await _send_one(msg.id, tenant_id) is None
    assert bot.enviados[0][0] == "sendMessage"


def _patch_cliente(monkeypatch) -> None:
    """El fixture trae un cliente de WhatsApp: en Telegram el id es el del usuario."""
    import core.tasks.outbound_send as mod
    from core.db.models import Customer

    real = mod.get_session

    class Sesion:
        def __init__(self) -> None:
            self.s = real()

        async def get(self, model, id_):
            if model is Customer:
                return SimpleNamespace(external_id="555")
            return await self.s.get(model, id_)

        async def __aenter__(self):
            await self.s.__aenter__()
            return self

        async def __aexit__(self, *a):
            return await self.s.__aexit__(*a)

    monkeypatch.setattr(mod, "get_session", Sesion)


@pytest.mark.parametrize(
    ("respuesta", "categoria", "marcado"),
    [
        (_error(401, "Unauthorized"), "AUTH", ["AUTH"]),
        (_error(403, "Forbidden: bot was blocked by the user"), "RECIPIENT", ["RECIPIENT"]),
        (_error(409, "Conflict: terminated by other getUpdates request"), "CONFIG", ["CONFIG"]),
        (_error(500, "Internal"), "TRANSIENT", ["TRANSIENT"]),
    ],
)
async def test_send_one_devuelve_el_error_clasificado(monkeypatch, respuesta, categoria, marcado):
    from core.tasks.outbound_send import _send_one

    bot = BotFalso()
    bot.respuesta = respuesta
    msg, tenant_id, calls = _con_telegram(monkeypatch, bot)
    _patch_cliente(monkeypatch)
    error = await _send_one(msg.id, tenant_id)
    assert isinstance(error, ProviderError) and error.category == categoria
    assert calls["marcados"] == marcado
    if categoria == "RECIPIENT":
        # Bloqueado: falla este envío, sin reintentos ni baja (la baja es solo OPT_OUT).
        assert decide_retry(error, attempts_made=0).action == "fail_permanent" and error.category != "OPT_OUT"


async def test_429_pausa_el_bot_lo_que_pide_telegram(monkeypatch):
    from core.tasks.outbound_send import _send_one

    bot = BotFalso()
    bot.respuesta = _error(429, "Too Many Requests: retry after 7", parameters={"retry_after": 7})
    msg, tenant_id, calls = _con_telegram(monkeypatch, bot)
    _patch_cliente(monkeypatch)
    error = await _send_one(msg.id, tenant_id)
    assert error is not None and error.category == "RATE_LIMIT" and error.retry_after == 7.0
    assert calls["pausas"] == [("123456789", 7.0)]
    assert decide_retry(error, attempts_made=0).countdown_s == 7.0


async def test_sin_lugar_en_el_ritmo_no_llama_a_telegram(monkeypatch):
    from core.tasks.outbound_send import _send_one

    bot = BotFalso()
    msg, tenant_id, calls = _con_telegram(monkeypatch, bot, espera=1.0)
    _patch_cliente(monkeypatch)
    error = await _send_one(msg.id, tenant_id)
    assert error is not None and error.category == "RATE_LIMIT" and error.retry_after == 1.0
    assert bot.enviados == [] and calls["release"] == 1


async def test_sin_credenciales_es_config_y_no_reintenta(monkeypatch):
    from core.tasks.outbound_send import _send_one

    bot = BotFalso()
    msg, tenant_id, _calls = _con_telegram(monkeypatch, bot, creds={})
    _patch_cliente(monkeypatch)
    error = await _send_one(msg.id, tenant_id)
    assert error is not None and error.category == "CONFIG" and bot.enviados == []
    assert decide_retry(error, attempts_made=0).action == "fail_permanent"


async def test_voz_rechazada_se_degrada_al_texto(monkeypatch):
    from core.mcp.host import mcp_host
    from core.tasks.outbound_send import _send_one

    bot = BotFalso()
    bot.respuesta = _error(400, "Bad Request: wrong file identifier/HTTP URL specified")
    msg, tenant_id, _calls = _con_telegram(monkeypatch, bot)
    _patch_cliente(monkeypatch)
    msg.media_type, msg.media_transcription, msg.content = "audio/tts", "Tu cita es mañana", "Tu cita es mañana"

    async def sintetizar(conversation_id, texto):
        return SimpleNamespace(mimetype="audio/ogg", audio_url="https://x/voz.ogg")

    monkeypatch.setattr(mcp_host, "synthesize_voice", sintetizar)
    assert await _send_one(msg.id, tenant_id) is None
    assert [m for m, _c in bot.enviados] == ["sendVoice", "sendMessage"]
    assert bot.enviados[1][1]["text"] == "Tu cita es mañana"
    assert msg.media_type is None and msg.media_url is None


async def test_un_canal_sin_voz_manda_el_texto(monkeypatch):
    """La voz no está atada a una conexión: si el adaptador no sabe mandar audio (`ChannelError`), sale el texto."""
    from core.gateway.channels.base import OutboundAdapter as Base
    from core.mcp.host import mcp_host
    from core.tasks.outbound_send import _send_one

    textos: list[str] = []

    class SinVoz(Base):
        provider = "otro"

        async def send_text(self, credentials, to_external_id, text):
            textos.append(text)
            return "id-1"

        async def send_image(self, credentials, to_external_id, image_url, caption=""):
            return "id-2"

    msg, tenant_id, _calls = _send_one_fixture(monkeypatch, adapter=SinVoz())
    msg.media_type, msg.media_transcription = "audio/tts", "Hola por voz"

    async def sintetizar(conversation_id, texto):
        return SimpleNamespace(mimetype="audio/ogg", audio_url="https://x/voz.ogg")

    monkeypatch.setattr(mcp_host, "synthesize_voice", sintetizar)
    assert await _send_one(msg.id, tenant_id) is None
    assert textos == ["Hola por voz"]
