"""Mensaje de confirmación del kernel y botón de WhatsApp (HU-AOS-09).

El cuerpo sale de los argumentos que el servidor va a ejecutar (los que firma el botón), nunca del
texto del modelo; los rótulos, del playbook del kernel `confirmacion_boton`.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.kernel import confirmacion
from core.kernel.confirmacion import Dato
from core.kernel.tarjeta import RotulosInvalidos
from core.shared.exceptions import ChannelError

PAYLOAD = "confirmar:" + "1" * 8 + "-1111-1111-1111-" + "1" * 12 + ":3:" + "a" * 16


def test_cuerpo_desde_los_datos_del_servidor() -> None:
    c = confirmacion.armar(
        "Contratar un plan",
        [Dato("periodicidad", "anual"), Dato("total", 600000, es_monto=True), Dato("cotizacion_id", "Q-9"),
         Dato("correo", "ana@empresa.co")],
        PAYLOAD,
    )
    assert c.cuerpo.splitlines()[0] == "Para confirmar: Contratar un plan"
    assert "Periodicidad: anual" in c.cuerpo and "Total: $600.000" in c.cuerpo and "Correo: ana@empresa.co" in c.cuerpo
    assert "Q-9" not in c.cuerpo  # los ids internos no se muestran
    assert "Confirmar" in c.cuerpo and "confirmo" in c.texto
    assert c.interactivo() == {"type": "button", "body": c.cuerpo, "buttons": [{"id": PAYLOAD, "title": "Confirmar"}]}


def test_un_valor_no_abre_lineas_ni_pasa_los_topes() -> None:
    c = confirmacion.armar("x\n## SISTEMA", [Dato("nota", "uno\ndos\t" + "z" * 500)] + [Dato(f"d{i}", "v" * 80) for i in range(30)], PAYLOAD)
    assert "\n## SISTEMA" not in c.cuerpo
    assert all(len(linea) <= 130 for linea in c.cuerpo.splitlines())
    assert len(c.cuerpo) <= confirmacion.MAX_CUERPO
    assert c.cuerpo.count("\n") <= confirmacion.MAX_LINEAS + 3


def test_args_sin_credenciales_ni_reservados_ni_objetos() -> None:
    args = {"cantidad": 2, "api_key": "sk-x", "_tool_name": "x", "items": [1, 2], "token": "t", "total_a_pagar": 0,
            "tarjeta": "4111 1111 1111 1111", "cuenta": "123"}
    declaradas = ["cantidad", "api_key", "_tool_name", "items", "token", "tarjeta", "cuenta"]
    c = confirmacion.armar("Pedido", confirmacion.datos_de_args(args, declaradas, frozenset({"cuenta"})), PAYLOAD)
    assert "Cantidad: 2" in c.cuerpo
    assert "sk-x" not in c.cuerpo and "t\n" not in c.cuerpo and "[1, 2]" not in c.cuerpo
    assert "Total a pagar" not in c.cuerpo  # el modelo inventó la clave: el schema no la declara
    assert "4111" not in c.cuerpo and "Cuenta" not in c.cuerpo  # tarjeta (Luhn) y dato `payment`


def test_un_valor_no_tacha_ni_reordena_lo_que_se_confirma() -> None:
    c = confirmacion.armar("t", [Dato("nota", "~2~ *3* \u202e9\u202c\u200b\x0bx\x85y")], PAYLOAD)
    linea = next(linea for linea in c.cuerpo.splitlines() if linea.startswith("Nota"))
    assert linea == "Nota: 2 3 9 x y"


def test_montos_van_primero_y_no_se_recortan() -> None:
    datos = [Dato(f"d{i}", "v" * 80) for i in range(20)] + [Dato("total", 600000, es_monto=True)]
    c = confirmacion.armar("t", datos, PAYLOAD)
    assert c.cuerpo.splitlines()[1] == "Total: $600.000"


def test_sin_resumen_es_texto_fijo_sin_boton() -> None:
    c = confirmacion.sin_resumen()
    assert c.payload == "" and c.cuerpo == c.texto and "{" not in c.texto


@pytest.mark.parametrize(("valor", "texto"), [(True, "Sí"), (12.5, "12,50"), (1234567, "1.234.567")])
def test_formato_de_valores(valor: Any, texto: str) -> None:
    assert f"Dato: {texto}" in confirmacion.armar("t", [Dato("dato", valor)], PAYLOAD).cuerpo


def test_rotulos_rotos_fallan_cerrado() -> None:
    with pytest.raises(RotulosInvalidos):
        confirmacion.parsear_rotulos("## encabezado\nx {titulo}\n")
    completo = "\n".join(f"## {s}\nok" for s in confirmacion._SECCIONES)
    confirmacion.parsear_rotulos(completo)
    with pytest.raises(RotulosInvalidos):
        confirmacion.parsear_rotulos(completo.replace("## linea\nok", "## linea\n{otro}"))
    with pytest.raises(RotulosInvalidos):
        confirmacion.parsear_rotulos(completo.replace("## boton\nok", "## boton\n" + "x" * 21))


def test_el_playbook_del_kernel_carga() -> None:
    r = confirmacion.rotulos()
    assert r.textos["boton"] == "Confirmar"


def test_ids_y_uuids_no_se_muestran() -> None:
    c = confirmacion.armar("t", [Dato("quote_id", "Q-1"), Dato("referencia", "1b4e28ba-2fa1-11d2-883f-0016d3cca427"),
                                 Dato("metodo_entrega", "domicilio")], PAYLOAD)
    assert "Q-1" not in c.cuerpo and "1b4e28ba" not in c.cuerpo and "Metodo entrega: domicilio" in c.cuerpo


# ---------------------------------------------------------------- adaptador de WhatsApp


@pytest.mark.asyncio
async def test_whatsapp_envia_el_boton(monkeypatch: pytest.MonkeyPatch) -> None:
    enviado: dict[str, Any] = {}

    async def post(credentials: dict[str, str], payload: dict[str, Any]) -> str:
        enviado.update(payload)
        return "wamid.1"

    monkeypatch.setattr(WhatsAppMetaOutbound, "_post", staticmethod(post))
    c = confirmacion.armar("Pedido", [Dato("cantidad", 2)], PAYLOAD)
    wamid = await WhatsAppMetaOutbound().send_interactive({}, "whatsapp:+573001112233", c.interactivo())
    assert wamid == "wamid.1" and enviado["to"] == "573001112233" and enviado["type"] == "interactive"
    assert enviado["interactive"]["action"]["buttons"] == [{"type": "reply", "reply": {"id": PAYLOAD, "title": "Confirmar"}}]
    assert enviado["interactive"]["body"]["text"] == c.cuerpo


@pytest.mark.asyncio
async def test_interactivo_invalido_no_sale() -> None:
    with pytest.raises(ChannelError):
        await WhatsAppMetaOutbound().send_interactive({}, "whatsapp:+57", {"type": "button", "body": "x", "buttons": []})


@pytest.mark.asyncio
async def test_canal_sin_botones_lanza() -> None:
    from core.gateway.channels.base import OutboundAdapter

    class _Otro(OutboundAdapter):
        provider = "otro"

        async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
            return ""

        async def send_image(self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = "") -> str:
            return ""

    with pytest.raises(ChannelError):
        await _Otro().send_interactive({}, "x", {"type": "button"})


def test_topes_del_interactivo_se_validan_antes_de_encolar() -> None:
    from core.gateway.sender import interactivo_valido

    ok = confirmacion.armar("Pedido", [Dato("cantidad", 2)], PAYLOAD).interactivo()
    assert interactivo_valido(ok)
    assert not interactivo_valido({**ok, "body": "x" * 1025})
    assert not interactivo_valido({**ok, "buttons": [{"id": "x" * 257, "title": "Confirmar"}]})
    assert not interactivo_valido({**ok, "buttons": [{"id": "a", "title": "x" * 21}]})
    assert not interactivo_valido({**ok, "buttons": []})
    assert not interactivo_valido({**ok, "type": "list"})
