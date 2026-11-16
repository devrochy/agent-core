"""Canal Telegram, entrada (ADR-0199, PR core 3): el adaptador `TelegramInbound` y los avisos por canal.

Puras, sin red ni base: el webhook de punta a punta está en `tests/integration/test_telegram_webhook_db.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from agentcore_channel_telegram import PROVIDER, InboundAdapter
from agentcore_channel_telegram.adapters import (
    TelegramInbound,
    callback_query_id,
    id_entrante,
    id_saliente,
    id_toque,
)

from core.gateway import avisos_canal as ac

BOT = "1234567890"
SECRETO = "s" * 43
ADAPTADOR = TelegramInbound()
PERSONA = {"id": 555, "is_bot": False, "first_name": "Ana", "last_name": "Gómez"}
CHAT = {"id": 555, "type": "private"}


def _update(**cuerpo: Any) -> dict[str, Any]:
    return {"update_id": 9001, **cuerpo}


def _mensaje(**campos: Any) -> dict[str, Any]:
    return {"message_id": 1, "from": PERSONA, "chat": CHAT, "date": 0, **campos}


def _uno(update: Any) -> Any:
    (mensaje,) = ADAPTADOR.parse_update(update, bot_id=BOT)
    return mensaje


def test_el_paquete_expone_el_adaptador_de_entrada():
    assert PROVIDER == "telegram" and InboundAdapter is TelegramInbound


# ------------------------------------------------------------------ secreto
@pytest.mark.parametrize(
    ("headers", "secreto", "ok"),
    [
        ({"X-Telegram-Bot-Api-Secret-Token": SECRETO}, SECRETO, True),
        ({"x-telegram-bot-api-secret-token": SECRETO}, SECRETO, True),
        ({"X-Telegram-Bot-Api-Secret-Token": "otro"}, SECRETO, False),
        ({}, SECRETO, False),
        ({"X-Telegram-Bot-Api-Secret-Token": ""}, "", False),
    ],
)
def test_el_secreto_del_webhook(headers, secreto, ok):
    assert ADAPTADOR.verify_request(b"{}", headers, secreto) is ok


# ------------------------------------------------------------------ mensajes
def test_texto_con_ids_globales():
    m = _uno(_update(message=_mensaje(text="Hola, ¿tienen envíos?")))
    assert (m.provider, m.channel) == ("telegram", "telegram")
    assert m.external_message_id == f"tg:{BOT}:in:555:1" == id_entrante(BOT, 555, 1)
    assert m.customer_external_id == "555" and m.connection_external_id == BOT
    assert m.customer_name == "Ana Gómez" and m.content == "Hola, ¿tienen envíos?"
    assert m.media == [] and m.interactive_id == ""


def test_start_llega_como_texto():
    assert _uno(_update(message=_mensaje(text="/start"))).content == "/start"


def test_el_mismo_mensaje_en_dos_bots_da_ids_distintos():
    a = ADAPTADOR.parse_update(_update(message=_mensaje(text="x")), bot_id="111")[0]
    b = ADAPTADOR.parse_update(_update(message=_mensaje(text="x")), bot_id="222")[0]
    assert a.external_message_id != b.external_message_id
    assert id_saliente("111", 555, 7) == "tg:111:out:555:7"


def test_foto_toma_la_mas_grande_que_se_puede_descargar_y_su_caption():
    fotos = [
        {"file_id": "chica", "width": 90, "height": 90, "file_size": 1000},
        {"file_id": "grande", "width": 1280, "height": 960, "file_size": 200_000},
        {"file_id": "enorme", "width": 4000, "height": 3000, "file_size": 30 * 1024 * 1024},
    ]
    m = _uno(_update(message=_mensaje(photo=fotos, caption="mi comprobante")))
    assert m.media == [{"type": "image/jpeg", "id": "grande"}] and m.content == "mi comprobante"


def test_nota_de_voz():
    m = _uno(_update(message=_mensaje(voice={"file_id": "v1", "duration": 3, "mime_type": "audio/ogg"})))
    assert m.media == [{"type": "audio/ogg", "id": "v1"}] and m.content == ""


@pytest.mark.parametrize(
    "update",
    [
        _update(message=_mensaje(text="hola", chat={"id": -100, "type": "group"})),
        _update(message=_mensaje(text="hola", chat={"id": -100, "type": "supergroup"})),
        _update(edited_message=_mensaje(text="editado")),
        _update(channel_post={"message_id": 1, "chat": {"id": -1, "type": "channel"}, "text": "x"}),
        _update(my_chat_member={"chat": CHAT}),
        _update(message=_mensaje(sticker={"file_id": "s"})),
        _update(message=_mensaje(document={"file_id": "d"})),
        _update(message=_mensaje(contact={"phone_number": "+573001112233", "user_id": 555})),
        _update(message=_mensaje(text="soy un bot", **{"from": {"id": 555, "is_bot": True}})),
        _update(message=_mensaje(text="chat de otro", chat={"id": 999, "type": "private"})),
        _update(message={**_mensaje(text="sin message_id"), "message_id": None}),
        {"update_id": "9001", "message": _mensaje(text="id que no es entero")},
        {"message": _mensaje(text="sin update_id")},
        "no es un dict",
        None,
    ],
)
def test_lo_que_no_se_procesa_da_lista_vacia(update):
    assert ADAPTADOR.parse_update(update, bot_id=BOT) == []


def test_sin_bot_id_no_procesa_nada():
    assert ADAPTADOR.parse_update(_update(message=_mensaje(text="hola")), bot_id="") == []
    assert ADAPTADOR.parse_inbound({"update": _update(message=_mensaje(text="hola"))}) == []
    assert len(ADAPTADOR.parse_inbound({"bot_id": BOT, "update": _update(message=_mensaje(text="hola"))})) == 1


# ------------------------------------------------------------------ botones
def _toque(data: str, **extra: Any) -> dict[str, Any]:
    teclado = {"inline_keyboard": [[{"text": "Plan Premium", "callback_data": "plan:premium"}], [{"text": "Otro", "callback_data": "x"}]]}
    return _update(
        callback_query={
            "id": "cbq-1",
            "from": PERSONA,
            "data": data,
            "message": _mensaje(text="Elige", reply_markup=teclado),
            **extra,
        }
    )


def test_el_toque_lleva_el_titulo_visible_y_el_id():
    update = _toque("plan:premium")
    m = _uno(update)
    assert m.content == "Plan Premium" and m.interactive_id == "plan:premium"
    assert m.external_message_id == f"tg:{BOT}:cb:cbq-1" == id_toque(BOT, "cbq-1")
    assert callback_query_id(update) == "cbq-1"


def test_un_toque_sin_boton_conocido_usa_su_data():
    assert _uno(_toque("tok:abc")).content == "tok:abc"


@pytest.mark.parametrize(
    "update",
    [
        _toque(""),
        _toque("plan:premium", **{"from": {"id": 1, "is_bot": True}}),
        _update(callback_query={"id": "c", "from": PERSONA, "data": "x"}),  # sin mensaje (inline mode)
        _update(callback_query={"from": PERSONA, "data": "x", "message": _mensaje(text="Elige")}),  # sin id
    ],
)
def test_toques_que_no_se_procesan(update):
    assert ADAPTADOR.parse_update(update, bot_id=BOT) == []


def test_callback_query_id_de_un_mensaje_es_vacio():
    assert callback_query_id(_update(message=_mensaje(text="hola"))) == ""
    assert callback_query_id(None) == ""


# ------------------------------------------------------------------ avisos por canal
def test_telegram_es_un_tipo_de_texto_sin_ventana():
    assert ac.TELEGRAM in ac.TIPOS_DE_TEXTO and ac.TELEGRAM not in ac.CON_VENTANA


@pytest.mark.parametrize("evento", [*sorted(ac.COBRANZA)[:2], "cita.recordatorio"])
def test_con_texto_un_aviso_sale_por_telegram_tambien_la_cobranza(evento):
    decision = ac.decidir_via_aviso(
        canal="telegram", proveedor="telegram", evento=evento, texto_definido=True, ventana_abierta=False
    )
    assert decision.via == ac.TEXTO and decision.tipo == "telegram"


def test_sin_texto_definido_se_omite_con_motivo():
    decision = ac.decidir_via_aviso(
        canal="telegram", proveedor="telegram", evento="cita.recordatorio", texto_definido=False, ventana_abierta=True
    )
    assert decision.via == ac.OMITIDO and decision.motivo == ac.SIN_RESPALDO
