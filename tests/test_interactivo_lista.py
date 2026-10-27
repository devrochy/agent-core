"""Mensajes interactivos de WhatsApp tipo lista en la salida (HU-INMO-03).

Valida los topes de Meta antes de encolar (`sender.interactivo_valido`), arma el payload de Graph
(`action.sections[].rows[]`) y comprueba que la respuesta de la lista ya la entiende el parseo
entrante (`list_reply`).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.gateway.sender import interactivo_valido
from core.shared.exceptions import ChannelError


def _lista(**cambios: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "list",
        "body": "¿Qué propiedad te interesa?",
        "button": "Ver opciones",
        "rows": [
            {"id": "inmueble:1", "title": "Apto Chipre", "description": "2 hab · 85 m²"},
            {"id": "inmueble:2", "title": "Casa Palogrande"},
        ],
    }
    return {**base, **cambios}


def _filas(n: int) -> list[dict[str, str]]:
    return [{"id": f"f:{i}", "title": f"Fila {i}"} for i in range(n)]


# ---------------------------------------------------------------- validación


def test_lista_valida_con_y_sin_descripcion() -> None:
    assert interactivo_valido(_lista())
    assert interactivo_valido(_lista(rows=[{"id": "a", "title": "A", "description": None}]))


def test_limites_exactos_de_lista_pasan_y_uno_mas_no() -> None:
    assert interactivo_valido(_lista(rows=_filas(10)))
    assert not interactivo_valido(_lista(rows=_filas(11)))
    assert not interactivo_valido(_lista(rows=[]))
    assert interactivo_valido(_lista(rows=[{"id": "x" * 200, "title": "A"}]))
    assert not interactivo_valido(_lista(rows=[{"id": "x" * 201, "title": "A"}]))
    assert interactivo_valido(_lista(rows=[{"id": "a", "title": "x" * 24}]))
    assert not interactivo_valido(_lista(rows=[{"id": "a", "title": "x" * 25}]))
    assert interactivo_valido(_lista(rows=[{"id": "a", "title": "A", "description": "x" * 72}]))
    assert not interactivo_valido(_lista(rows=[{"id": "a", "title": "A", "description": "x" * 73}]))
    assert interactivo_valido(_lista(button="x" * 20))
    assert not interactivo_valido(_lista(button="x" * 21))
    assert interactivo_valido(_lista(body="x" * 1024))
    assert not interactivo_valido(_lista(body="x" * 1025))


@pytest.mark.parametrize(
    "cambios",
    [
        {"body": ""},
        {"body": "   "},
        {"body": 5},
        {"button": ""},
        {"button": "  "},
        {"button": None},
        {"rows": None},
        {"rows": "no-es-lista"},
        {"rows": ["fila"]},
        {"rows": [{"id": "", "title": "A"}]},
        {"rows": [{"id": 7, "title": "A"}]},
        {"rows": [{"id": "a", "title": ""}]},
        {"rows": [{"id": "a", "title": "   "}]},
        {"rows": [{"id": "a", "title": 3}]},
        {"rows": [{"id": "a", "title": "A", "description": ""}]},
        {"rows": [{"id": "a", "title": "A", "description": 9}]},
        # Meta exige ids de fila únicos.
        {"rows": [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]},
    ],
)
def test_lista_invalida_se_rechaza(cambios: dict[str, Any]) -> None:
    assert not interactivo_valido(_lista(**cambios))


def test_lista_sin_claves_o_con_forma_de_boton_se_rechaza() -> None:
    sin_boton = _lista()
    del sin_boton["button"]
    assert not interactivo_valido(sin_boton)
    sin_filas = _lista()
    del sin_filas["rows"]
    assert not interactivo_valido(sin_filas)
    # La forma de botones con type "list" no es una lista, y viceversa.
    assert not interactivo_valido({"type": "list", "body": "x", "buttons": [{"id": "a", "title": "A"}]})
    assert not interactivo_valido({**_lista(), "type": "button"})
    assert not interactivo_valido({**_lista(), "type": "carousel"})


def test_botones_siguen_igual() -> None:
    ok = {"type": "button", "body": "¿Confirmas?", "buttons": [{"id": "si", "title": "Sí"}]}
    assert interactivo_valido(ok)
    assert not interactivo_valido({**ok, "buttons": [{"id": "a", "title": "A"}] * 4})


# ---------------------------------------------------------------- payload de Graph


async def _capturar(monkeypatch: pytest.MonkeyPatch, interactive: dict[str, Any]) -> dict[str, Any]:
    enviado: dict[str, Any] = {}

    async def post(credentials: dict[str, str], payload: dict[str, Any]) -> str:
        enviado.update(payload)
        return "wamid.lista"

    monkeypatch.setattr(WhatsAppMetaOutbound, "_post", staticmethod(post))
    wamid = await WhatsAppMetaOutbound().send_interactive({}, "whatsapp:+573001112233", interactive)
    assert wamid == "wamid.lista"
    return enviado


@pytest.mark.asyncio
async def test_whatsapp_arma_la_lista(monkeypatch: pytest.MonkeyPatch) -> None:
    entrada = _lista()
    original = copy.deepcopy(entrada)
    payload = await _capturar(monkeypatch, entrada)
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "573001112233",
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "¿Qué propiedad te interesa?"},
            "action": {
                "button": "Ver opciones",
                "sections": [
                    {
                        "rows": [
                            {"id": "inmueble:1", "title": "Apto Chipre", "description": "2 hab · 85 m²"},
                            {"id": "inmueble:2", "title": "Casa Palogrande"},
                        ]
                    }
                ],
            },
        },
    }
    assert entrada == original  # no muta el payload guardado en messages.interactive


@pytest.mark.asyncio
async def test_descripcion_vacia_o_nula_no_viaja(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = await _capturar(
        monkeypatch,
        _lista(rows=[{"id": "a", "title": "A", "description": ""}, {"id": "b", "title": "B", "description": None}]),
    )
    filas = payload["interactive"]["action"]["sections"][0]["rows"]
    assert filas == [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]


@pytest.mark.asyncio
async def test_lista_de_diez_filas_va_en_una_sola_seccion(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = await _capturar(monkeypatch, _lista(rows=_filas(10)))
    secciones = payload["interactive"]["action"]["sections"]
    assert len(secciones) == 1 and len(secciones[0]["rows"]) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("cambios", [{"rows": []}, {"rows": None}, {"button": ""}, {"body": ""}])
async def test_lista_incompleta_no_sale(cambios: dict[str, Any]) -> None:
    with pytest.raises(ChannelError):
        await WhatsAppMetaOutbound().send_interactive({}, "whatsapp:+57", _lista(**cambios))


# ---------------------------------------------------------------- respuesta entrante


def test_respuesta_de_lista_llega_como_interactive_id() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "123"},
                            "contacts": [{"wa_id": "573001112233", "profile": {"name": "Ana"}}],
                            "messages": [
                                {
                                    "id": "wamid.in",
                                    "from": "573001112233",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "list_reply",
                                        "list_reply": {"id": "inmueble:1", "title": "Apto Chipre", "description": "x"},
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    [mensaje] = WhatsAppMetaInbound().parse_inbound(payload)
    assert mensaje.interactive_id == "inmueble:1" and mensaje.content == "Apto Chipre"
    assert len("x" * 200) <= 256  # el tope de id de fila (200) cabe en el recorte del entrante
