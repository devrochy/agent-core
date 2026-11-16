"""Canal Telegram, medios y contacto (ADR-0199, PR core 5): descarga por `file_id` y teléfono compartido.

Sin red ni base: la Bot API es un `httpx.MockTransport`. La ingesta y el consumer contra Postgres están en
`tests/integration/test_telegram_medios_db.py`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agentcore_channel_telegram.adapters import TelegramInbound, telefono_propio

from core.media.errors import MediaProviderError
from core.media.telegram_media import descargar_media_telegram

TOKEN = "123456789:AAH" + "x" * 32
PERSONA = {"id": 555, "is_bot": False, "first_name": "Ana"}


def _transporte(*, file_size: int = 10, contenido: bytes = b"OggS-audio", status_get_file: int = 200, file_path: str = "voice/file_1.oga"):
    llamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            if status_get_file != 200:
                return httpx.Response(status_get_file, json={"ok": False, "error_code": status_get_file, "description": "x"})
            return httpx.Response(
                200, json={"ok": True, "result": {"file_id": "f1", "file_size": file_size, "file_path": file_path}}
            )
        return httpx.Response(200, content=contenido)

    return httpx.MockTransport(handler), llamadas


# ------------------------------------------------------------------ descarga
async def test_descarga_por_file_id_con_el_token_de_la_conexion():
    transporte, llamadas = _transporte()
    media = await descargar_media_telegram("f1", TOKEN, mime_esperado="audio/ogg", transport=transporte)
    assert media.contenido == b"OggS-audio" and media.mime_type == "audio/ogg" and media.bytes == 10
    assert llamadas == [f"/bot{TOKEN}/getFile", f"/file/bot{TOKEN}/voice/file_1.oga"]


async def test_el_tipo_sale_de_la_extension_si_el_webhook_no_lo_dijo():
    transporte, _ = _transporte(file_path="photos/file_2.jpg", contenido=b"\xff\xd8jpg")
    media = await descargar_media_telegram("f2", TOKEN, transport=transporte)
    assert media.mime_type == "image/jpeg"


async def test_un_archivo_grande_no_se_descarga():
    transporte, llamadas = _transporte(file_size=21 * 1024 * 1024)
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_telegram("f1", TOKEN, mime_esperado="audio/ogg", transport=transporte)
    assert exc.value.code == "media_demasiado_grande" and not exc.value.transient
    assert len(llamadas) == 1, "solo getFile; el archivo no se baja"


@pytest.mark.parametrize(("status", "transitorio", "codigo"), [(401, False, "media_sin_token"), (502, True, "media_red")])
async def test_errores_de_telegram_se_clasifican(status, transitorio, codigo):
    transporte, _ = _transporte(status_get_file=status)
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_telegram("f1", TOKEN, mime_esperado="audio/ogg", transport=transporte)
    assert exc.value.transient is transitorio and exc.value.code == codigo
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(("file_id", "token", "codigo"), [("", TOKEN, "media_sin_id"), ("f1", "", "media_sin_token")])
async def test_sin_file_id_o_sin_token_no_llama_a_telegram(file_id, token, codigo):
    transporte, llamadas = _transporte()
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_telegram(file_id, token, transport=transporte)
    assert exc.value.code == codigo and llamadas == []


# ------------------------------------------------------------------ teléfono compartido
def _con_contacto(**contacto: Any) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 7,
            "from": PERSONA,
            "chat": {"id": 555, "type": "private"},
            "contact": {"first_name": "Ana", **contacto},
        },
    }


@pytest.mark.parametrize("numero", ["573001112233", "+573001112233", "+57 300 111 2233"])
def test_el_contacto_propio_llega_como_shared_phone_en_e164(numero):
    (m,) = TelegramInbound().parse_update(_con_contacto(phone_number=numero, user_id=555), bot_id="1")
    assert m.shared_phone == "+573001112233" and m.content == "+573001112233"


@pytest.mark.parametrize(
    "contacto",
    [
        {"phone_number": "+573009998877", "user_id": 999},  # reenviado: de otra persona
        {"phone_number": "+573009998877"},  # sin user_id: no se puede comprobar
        {"phone_number": "123", "user_id": 555},  # no es un número válido
    ],
)
def test_un_contacto_que_no_es_del_remitente_no_se_guarda(contacto):
    assert TelegramInbound().parse_update(_con_contacto(**contacto), bot_id="1") == []
    assert telefono_propio(_con_contacto(**contacto)["message"], PERSONA) is None


def test_un_texto_normal_no_trae_telefono():
    update = {"update_id": 2, "message": {"message_id": 8, "from": PERSONA, "chat": {"id": 555, "type": "private"}, "text": "hola"}}
    (m,) = TelegramInbound().parse_update(update, bot_id="1")
    assert m.shared_phone is None


def test_sentry_tapa_el_telefono_compartido():
    import json

    from core.observability.sentry_setup import _event_scrubber

    event = {"request": {"data": {"shared_phone": "+573001112233", "contact": {"phone_number": "+573001112233"}}}}
    _event_scrubber().scrub_event(event)
    assert "3001112233" not in json.dumps(event, default=str)
