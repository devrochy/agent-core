"""HU-CONN-26 «Pedir» (ADR-0199 §10): la tool del core `pedir_telefono`, la capacidad `pedir_contacto` y el teclado
de respuesta `request_contact` de Telegram.

Sin red ni base: la Bot API es un `httpx.MockTransport` y la base se reemplaza con dobles. Lo que necesita Postgres y
Redis (la tool encola, el adaptador manda el teclado y el contacto compartido llena `contact_phone`) está en
`tests/integration/test_telegram_pedir_telefono_db.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from agentcore_channel_telegram.adapters import teclado_contacto
from test_telegram_salida import CREDS, BotFalso

from core.gateway.channels.base import ProviderError
from core.gateway.connection_types import (
    CAPACIDADES_POR_TIPO,
    capacidades_de,
    capacidades_del_canal,
)
from core.gateway.sender import contacto_valido, interactivo_valido
from core.mcp.types import ToolContext
from core.tools import core_skill
from core.tools.core_skill import CoreSkill, hay_canal_que_pide_contacto

CONTACTO = {"type": "contacto", "body": "Para buscar tu cuenta necesito tu número.", "button": "Compartir mi teléfono"}


# ------------------------------------------------------------------ capacidad
def test_solo_telegram_pide_el_contacto():
    assert {t for t, c in CAPACIDADES_POR_TIPO.items() if c.pedir_contacto} == {"telegram"}
    assert capacidades_de("telegram", "telegram").admite("contacto")
    assert not capacidades_del_canal("whatsapp").admite("contacto")
    assert not capacidades_del_canal("test").admite("contacto"), "el playground no pinta el teclado de contacto"


# ------------------------------------------------------------------ validación del sender
def test_contacto_valido_y_sus_topes():
    assert contacto_valido(CONTACTO)
    assert not interactivo_valido(CONTACTO), "`interactivo_valido` son los topes de Meta: Meta no tiene este tipo"
    assert not contacto_valido({**CONTACTO, "body": "  "})
    assert not contacto_valido({**CONTACTO, "button": ""})
    assert not contacto_valido({**CONTACTO, "button": "x" * 65})
    assert not contacto_valido({**CONTACTO, "body": "x" * 1025})
    assert not contacto_valido({**CONTACTO, "buttons": [{"id": "a", "title": "b"}]})
    assert not contacto_valido({**CONTACTO, "type": "button"})


# ------------------------------------------------------------------ adaptador
async def test_contacto_sale_como_teclado_de_respuesta_de_un_solo_uso():
    bot = BotFalso()
    assert await bot.adaptador.send_interactive(CREDS, "555", CONTACTO) == "tg:123456789:out:555:101"
    [(metodo, cuerpo)] = bot.enviados
    assert metodo == "sendMessage" and cuerpo["text"] == CONTACTO["body"]
    assert cuerpo["reply_markup"] == {
        "keyboard": [[{"text": "Compartir mi teléfono", "request_contact": True}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }


def test_contacto_sin_etiqueta_es_error_de_contenido():
    with pytest.raises(ProviderError) as exc:
        teclado_contacto({"type": "contacto", "body": "Hola", "button": "  "})
    assert exc.value.category == "CONTENT"


# ------------------------------------------------------------------ predicado de oferta
@pytest.mark.parametrize(
    ("tipos", "habilitado", "esperado"),
    [
        ({"telegram"}, True, True),
        ({"telegram", "whatsapp_meta"}, True, True),
        ({"telegram"}, False, False),  # kill-switch apagado: la salida no enviaría nada
        ({"whatsapp_meta", "whatsapp_waweb"}, True, False),
        (set(), True, False),
    ],
)
async def test_se_ofrece_solo_con_una_conexion_que_pide_contacto(monkeypatch, tipos, habilitado, esperado):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "telegram_habilitado", habilitado)
    assert await hay_canal_que_pide_contacto(tipos) is esperado


def test_la_tool_es_del_core_con_parametros_estrictos():
    [tool] = [t for t in CoreSkill().get_tools() if t.name == "pedir_telefono"]
    assert tool.always_available and tool.side_effects and tool.simular is not None
    assert tool.connection_predicate is hay_canal_que_pide_contacto
    assert tool.parameters["required"] == ["texto", "etiqueta"]
    assert tool.parameters["additionalProperties"] is False


# ------------------------------------------------------------------ handler
TENANT = uuid.uuid4()
CONV = uuid.uuid4()
CLIENTE = uuid.uuid4()


def _ctx(channel: str = "telegram", clave: str = "call_1") -> ToolContext:
    return ToolContext(conversation_id=CONV, customer_id=CLIENTE, idempotency_key=f"{CONV}:{clave}", channel=channel)


class _Sesion:
    def __init__(self, cliente: Any) -> None:
        self._cliente = cliente

    async def __aenter__(self) -> _Sesion:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, modelo: Any, _id: Any) -> Any:
        from core.db.models import Conversation

        if modelo is Conversation:
            return SimpleNamespace(customer_id=CLIENTE)
        return self._cliente


@pytest.fixture
def entorno(monkeypatch) -> dict[str, Any]:
    from core.gateway import sender

    estado: dict[str, Any] = {
        "cliente": SimpleNamespace(tenant_id=TENANT, contact_phone=None),
        "en_cola": set(),
        "envios": [],
        "resultado": True,
    }

    async def enviar(conversation_id, interactive, **kwargs):
        estado["envios"].append((conversation_id, interactive, kwargs))
        if estado["resultado"]:
            estado["en_cola"].add(kwargs["client_message_id"])
        return estado["resultado"]

    async def en_cola(id_mensaje: str) -> bool:
        return id_mensaje in estado["en_cola"]

    monkeypatch.setattr(core_skill, "get_session", lambda: _Sesion(estado["cliente"]))
    monkeypatch.setattr(core_skill, "get_current_tenant_id", lambda: TENANT)
    monkeypatch.setattr(core_skill, "_pedido_de_contacto_en_cola", en_cola)
    monkeypatch.setattr(sender, "send_interactive_to_conversation", enviar)
    return estado


ARGS = {"texto": CONTACTO["body"], "etiqueta": CONTACTO["button"]}


async def test_encola_el_boton_sin_degradar_y_con_id_estable(entorno):
    assert await CoreSkill()._pedir_telefono(ARGS, _ctx()) == {"status": "enviado"}
    [(conv, interactivo, kwargs)] = entorno["envios"]
    assert conv == CONV and interactivo == CONTACTO
    assert kwargs["sin_degradar"] is True and kwargs["client_message_id"] == f"pedir_tel:{CONV}:call_1"


async def test_la_misma_llamada_no_lo_manda_dos_veces(entorno):
    assert (await CoreSkill()._pedir_telefono(ARGS, _ctx()))["status"] == "enviado"
    assert await CoreSkill()._pedir_telefono(ARGS, _ctx()) == {"status": "ya_enviado"}
    assert len(entorno["envios"]) == 1


async def test_una_carrera_con_la_misma_clave_es_ya_enviado(entorno, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from core.gateway import sender

    async def choca(*_a, **_k):
        raise IntegrityError("insert", {}, Exception('duplicate key "uq_messages_tenant_client_message_id"'))

    monkeypatch.setattr(sender, "send_interactive_to_conversation", choca)
    assert await CoreSkill()._pedir_telefono(ARGS, _ctx()) == {"status": "ya_enviado"}


async def test_otra_violacion_de_la_base_no_se_tapa(entorno, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from core.gateway import sender

    async def choca(*_a, **_k):
        raise IntegrityError("insert", {}, Exception('violates check constraint "ck_messages_interactive_objeto"'))

    monkeypatch.setattr(sender, "send_interactive_to_conversation", choca)
    with pytest.raises(IntegrityError):
        await CoreSkill()._pedir_telefono(ARGS, _ctx())


@pytest.mark.parametrize("canal", ["whatsapp", "messenger", "web", "test"])
async def test_un_canal_sin_la_capacidad_no_envia_nada(entorno, canal):
    resultado = await CoreSkill()._pedir_telefono(ARGS, _ctx(channel=canal))
    assert resultado["error"] == "canal_sin_pedir_contacto" and resultado["mensaje"]
    assert entorno["envios"] == []


async def test_con_telefono_no_lo_pide_ni_lo_devuelve(entorno):
    entorno["cliente"].contact_phone = "+573001112233"
    resultado = await CoreSkill()._pedir_telefono(ARGS, _ctx())
    assert resultado == {"status": "ya_tiene_telefono"} and "3001112233" not in str(resultado)
    assert entorno["envios"] == []


async def test_un_cliente_de_otro_negocio_no_se_toca(entorno):
    entorno["cliente"].tenant_id = uuid.uuid4()
    assert await CoreSkill()._pedir_telefono(ARGS, _ctx()) == {"error": "cliente_no_encontrado"}
    assert entorno["envios"] == []


@pytest.mark.parametrize("args", [{"texto": " ", "etiqueta": "Compartir"}, {"texto": "Hola", "etiqueta": ""}, {}])
async def test_entrada_vacia_es_resultado_no_excepcion(entorno, args):
    assert await CoreSkill()._pedir_telefono(args, _ctx()) == {"error": "texto_o_etiqueta_vacios"}


async def test_si_no_se_encola_lo_dice_sin_lanzar(entorno):
    entorno["resultado"] = False
    resultado = await CoreSkill()._pedir_telefono(ARGS, _ctx())
    assert resultado["error"] == "no_enviado" and resultado["mensaje"]


async def test_en_el_playground_se_simula_y_lo_dice():
    from core.tools.sandbox import simular_pedir_telefono

    resultado = await simular_pedir_telefono(ARGS, _ctx(channel="test"))
    assert resultado["status"] == "simulado" and "SIMULADO" in resultado["message"]
