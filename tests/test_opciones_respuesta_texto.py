"""Pista C2 · la respuesta de texto a un menú numerado (SPEC-40, ADR-0172 adenda), sin base.

- `respuesta_texto.elegir`: qué texto es una elección inequívoca («1», «1.», título exacto) y qué NO («1 por favor»,
  fuera de rango, título parcial, ambiguo).
- `puede_ser_respuesta_de_menu`: las guardas baratas del consumer (canal, botón, adjunto, largo).
- `opcion_elegida_por_texto`: las guardas con base simuladas en el borde (`activaciones`): último saliente,
  vía `text`, conversación, vencimiento; y que el payload que sale lo firma el servidor y valida con la misma firma.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from core.kernel.opciones import payload, respuesta_texto
from core.kernel.opciones.activaciones import Envio
from core.kernel.opciones.respuesta_texto import elegir
from core.orchestrator import opciones_toque
from core.orchestrator.opciones_toque import opcion_elegida_por_texto, puede_ser_respuesta_de_menu

TITULOS = ["Ver estado", "Otra consulta", "Hablar con alguien"]


# ---------------------------------------------------------------- elegir
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1", 0),
        ("2", 1),
        ("3", 2),
        ("1.", 0),
        ("2)", 1),
        (" 1 ", 0),
        ("\t3\n", 2),
        ("1 .", 0),
        ("Ver estado", 0),
        ("ver estado", 0),
        ("VER ESTADO", 0),
        ("  ver   estado  ", 0),
        ("Otra\nconsulta", 1),
        ("hablar con alguien", 2),
    ],
)
def test_una_eleccion_inequivoca_se_reconoce(texto: str, esperado: int) -> None:
    assert elegir(texto, TITULOS) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "1 por favor",
        "la 1",
        "quiero la 1",
        "1 y 2",
        "1,2",
        "uno",
        "0",
        "4",  # fuera de rango (3 opciones)
        "99",
        "100",
        "01",  # sin ceros a la izquierda
        "-1",
        "+1",
        "1.5",
        "1️⃣",
        "\uff11",  # dígito de ancho completo: no es un número del menú
        "\u0662",  # dígito árabe
        "ver",  # título parcial
        "ver estado por favor",
        "Ver estado.",  # exacto es exacto (salvo mayúsculas y espacios)
        "ver-estado",
        "",
        "   ",
        "x" * 101,
    ],
)
def test_lo_que_no_es_una_eleccion_exacta_no_se_interpreta(texto: str) -> None:
    assert elegir(texto, TITULOS) is None


def test_sin_opciones_no_hay_eleccion() -> None:
    assert elegir("1", []) is None
    assert elegir("hola", []) is None


def test_un_numero_nunca_se_toma_como_titulo() -> None:
    """Con 3 opciones, «5» está fuera de rango aunque exista una opción titulada «5»: la forma de número manda."""
    assert elegir("5", ["5", "Dos", "Tres"]) is None
    assert elegir("2", ["5", "Dos", "Tres"]) == 1


def test_dos_titulos_iguales_al_normalizar_son_ambiguos() -> None:
    assert elegir("si", ["si", "SI", "No"]) is None  # mayúsculas indiferentes: dos opciones coinciden
    assert elegir("1", ["si", "SI", "No"]) == 0  # el número sigue valiendo
    assert elegir("si", ["Sí", "SI", "No"]) == 1  # los acentos sí distinguen: solo «SI» coincide
    assert elegir("sí", ["Sí", "SI", "No"]) == 0


def test_el_tope_de_largo_es_el_del_titulo_de_la_definicion() -> None:
    from core.kernel.opciones.definicion import OpcionDeLaPlantilla

    maximo = OpcionDeLaPlantilla.model_fields["titulo"].metadata[0].max_length
    assert respuesta_texto.MAX_LARGO_ELECCION == maximo
    titulo = "a" * maximo
    assert elegir(titulo, [titulo, "b"]) == 0


# ---------------------------------------------------------------- guardas baratas
def _conv(canal: str = "web") -> Any:
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), channel=canal)


def _campos(contenido: str = "1", **extra: str) -> dict[str, str]:
    return {"content": contenido, "provider": "web", "media": "[]", **extra}


@pytest.mark.parametrize(
    ("canal", "proveedor", "esperado"),
    [
        ("web", "web", True),
        ("whatsapp", "waweb", True),
        ("messenger", "meta", True),
        ("instagram", "meta", True),
        ("whatsapp", "meta", False),  # tiene botones: el cliente los toca
        ("whatsapp", "", False),  # proveedor desconocido: fail-closed
        ("whatsapp", "twilio", False),
        ("email", "smtp", False),
        ("test", "internal", False),
        ("sms", "x", False),
    ],
)
def test_solo_los_canales_con_respuesta_numerica_la_interpretan(canal: str, proveedor: str, esperado: bool) -> None:
    assert puede_ser_respuesta_de_menu(_conv(canal), _campos(provider=proveedor)) is esperado


def test_un_boton_un_adjunto_o_un_texto_vacio_o_largo_no_son_una_respuesta() -> None:
    c = _conv("web")
    assert puede_ser_respuesta_de_menu(c, _campos())
    assert not puede_ser_respuesta_de_menu(c, _campos(interactive_id="proc:pagar"))
    assert not puede_ser_respuesta_de_menu(c, _campos(media='[{"type": "audio/ogg"}]'))
    assert not puede_ser_respuesta_de_menu(c, _campos(""))
    assert not puede_ser_respuesta_de_menu(c, _campos("   "))
    assert not puede_ser_respuesta_de_menu(c, _campos("x" * 101))
    assert puede_ser_respuesta_de_menu(c, {"content": "1", "provider": "web"}), "sin el campo media (la web no lo manda)"


# ---------------------------------------------------------------- guardas con base simulada en el borde
SEND = uuid.uuid4()


def _definicion(n: int = 3) -> dict[str, Any]:
    opciones = [
        {
            "id": f"op{i}",
            "titulo": TITULOS[i % 3] if i < 3 else f"Opción {i}",
            "accion": {"tool": "t", "args_fijos": {}, "args_contexto": {}, "confirmar": False},
            "respuesta_ok": "ok",
            "respuesta_error": "error",
            "result_keys": [],
        }
        for i in range(n)
    ]
    return {"body": "Hola", "footer": None, "variables": [], "exclusivo": True, "ttl_horas": 24, "opciones": opciones}


def _envio(conv: Any, *, via: str = "text", vence_en: timedelta = timedelta(hours=3), conversation_id: uuid.UUID | None = None) -> Envio:
    return Envio(
        send_id=SEND, conversation_id=conversation_id or conv.id, template_id=uuid.uuid4(), template_key="menu",
        template_retirada=False, version_id=uuid.uuid4(), version_n=1, version_retirada=False, definition=_definicion(),
        context={}, expires_at=datetime.now(UTC) + vence_en, via=via,
    )


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """El borde simulado: `activaciones` devuelve lo que el test fije."""
    estado: dict[str, Any] = {"ultimo": SEND, "envio": None, "consultas": []}

    async def ultimo(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> uuid.UUID | None:
        estado["consultas"].append("ultimo")
        return estado["ultimo"]

    async def cargar(tenant_id: uuid.UUID, send_id: uuid.UUID) -> Envio | None:
        estado["consultas"].append("cargar")
        return estado["envio"]

    monkeypatch.setattr(opciones_toque.activaciones, "envio_del_ultimo_saliente", ultimo)
    monkeypatch.setattr(opciones_toque.activaciones, "cargar_envio", cargar)
    return estado


async def test_un_menu_de_texto_vigente_y_ultimo_se_resuelve_con_un_payload_firmado_por_el_servidor(base) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = _envio(conv)

    firmado = await opcion_elegida_por_texto(conv, _campos("2"))

    leido = payload.leer(firmado)
    assert leido is not None and (leido.tipo, leido.option_id, leido.send_id) == ("opt", "op1", SEND)
    assert payload.firma_valida(leido, payload.secreto_del_servidor(), conv.tenant_id, conv.id)
    # La firma ata el negocio y la conversación: en otra conversación no vale.
    assert not payload.firma_valida(leido, payload.secreto_del_servidor(), conv.tenant_id, uuid.uuid4())


async def test_el_titulo_exacto_elige_la_misma_opcion_que_su_numero(base) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = _envio(conv)
    por_numero = await opcion_elegida_por_texto(conv, _campos("3"))
    por_titulo = await opcion_elegida_por_texto(conv, _campos("  HABLAR con alguien "))
    assert por_numero == por_titulo and por_numero is not None


@pytest.mark.parametrize("texto", ["1 por favor", "4", "0", "hola", "ver"])
async def test_un_texto_que_no_es_una_eleccion_sigue_al_agente(base, texto: str) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = _envio(conv)
    assert await opcion_elegida_por_texto(conv, _campos(texto)) is None


async def test_si_el_ultimo_saliente_no_es_un_menu_no_se_consulta_nada_mas(base) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["ultimo"] = None
    base["envio"] = _envio(conv)
    assert await opcion_elegida_por_texto(conv, _campos("1")) is None
    assert base["consultas"] == ["ultimo"]


async def test_un_menu_vencido_no_se_interpreta(base) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = _envio(conv, vence_en=timedelta(seconds=-1))
    assert await opcion_elegida_por_texto(conv, _campos("1")) is None


@pytest.mark.parametrize("via", ["interactive", "template"])
async def test_un_envio_con_botones_no_se_contesta_con_texto(base, via: str) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = _envio(conv, via=via)
    assert await opcion_elegida_por_texto(conv, _campos("1")) is None


async def test_un_envio_de_otra_conversacion_no_se_interpreta(base) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = _envio(conv, conversation_id=uuid.uuid4())
    assert await opcion_elegida_por_texto(conv, _campos("1")) is None


async def test_un_envio_inexistente_o_con_definicion_ilegible_no_se_interpreta(base) -> None:  # type: ignore[no-untyped-def]
    conv = _conv()
    base["envio"] = None
    assert await opcion_elegida_por_texto(conv, _campos("1")) is None
    roto = _envio(conv)
    base["envio"] = Envio(**{**roto.__dict__, "definition": {"body": 3}})
    assert await opcion_elegida_por_texto(conv, _campos("1")) is None
