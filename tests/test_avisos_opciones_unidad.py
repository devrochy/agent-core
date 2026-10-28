"""ADR-0178 D3 · puente aviso por evento → plantilla con opciones: las reglas, sin base de datos.

El envío de opciones (`enviar_opciones`) es el borde que se dobla; `tests/integration/test_avisos_opciones_db.py` cubre
el camino completo con Postgres y Redis reales. Aquí:

- qué claves y qué datos del evento valen (puro);
- cuándo se intenta y cuándo se cae al camino de D2/legacy (bandera, canal, cobranza, conversación activa, políticas);
- que un envío ya hecho no se duplica y que un resultado ambiguo se reintenta;
- la regresión de la bandera APAGADA: con `option_template` mapeado y `por_canal=False`, los 8 eventos se comportan
  igual que antes de D3.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from test_avisos_canal_unidad import (  # noqa: F401  (el doble de `send_event_notice` y sus ayudas)
    CONTEXTO,
    CONTEXTO_COMPLETO,
    EVENTO,
    TENANT,
    _conversacion,
    _enviar,
    entorno,
)

import core.gateway.avisos_canal as ac
import core.gateway.avisos_opciones as puente
import core.gateway.event_notices as notices
from core.kernel.mensajes import COBRANZA, SIN_OPCIONES
from core.kernel.opciones import contexto
from core.kernel.opciones import envio as ev
from core.shared.tenant import tenant_scope

CLAVE = "recordatorio_cita"
MAPA = {EVENTO: {"fallback_text_by_channel": {ac.WEB: "Texto web {cliente.nombre}"}, "option_template": CLAVE}}


# ---------------------------------------------------------------- puro
@pytest.mark.parametrize(
    ("entrada", "esperada"),
    [
        ({"option_template": CLAVE}, CLAVE),
        ({"option_template": f"  {CLAVE} "}, CLAVE),
        ({"option_template": ""}, None),
        ({"option_template": "Con Espacios"}, None),
        ({"option_template": "a" * 41}, None),
        ({"option_template": 3}, None),
        ({"option_template": None}, None),
        ({}, None),
        (None, None),
        ("no es un dict", None),
    ],
)
def test_la_clave_de_opciones_se_lee_con_su_forma(entrada: Any, esperada: str | None) -> None:
    assert puente.clave_de_opciones(EVENTO, entrada) == esperada


def test_un_evento_desconocido_o_de_cobranza_nunca_tiene_puente() -> None:
    assert puente.clave_de_opciones("evento.inventado", {"option_template": CLAVE}) is None
    for evento in COBRANZA:
        assert puente.clave_de_opciones(evento, {"option_template": CLAVE}) is None
    for evento in notices.EVENTOS:
        assert (puente.clave_de_opciones(evento, {"option_template": CLAVE}) is None) == (evento in SIN_OPCIONES)


def test_los_datos_del_evento_son_solo_rutas_del_evento_que_estan_en_la_lista_cerrada() -> None:
    datos = puente.datos_del_evento(
        EVENTO,
        {
            "cliente": {"nombre": "Mallory"},  # base: la resuelve el envío desde la conversación, no el handler
            "cita": {"titulo": "Corte\n  de pelo", "fecha": "2026-11-02", "hora": None, "zona": {"x": 1}, "enlace": ""},
            "cargo": {"monto": "$1"},  # ruta de otro evento: no existe aquí
            "extra": "x",
        },
    )
    assert datos == {"cita.titulo": "Corte de pelo", "cita.fecha": "2026-11-02"}  # saneado; vacío, None y objetos fuera
    assert set(datos) <= set(contexto.RUTAS_DE_DATOS)


def test_los_eventos_de_cobranza_no_aportan_datos() -> None:
    for evento in COBRANZA:
        assert puente.datos_del_evento(evento, CONTEXTO_COMPLETO) == {}


# ---------------------------------------------------------------- cuándo se intenta
@pytest.fixture
def opciones(monkeypatch, entorno):  # type: ignore[no-untyped-def]  # noqa: F811
    estado: dict[str, Any] = {
        "llamadas": [], "resultado": ev.ResultadoEnvio(True, ev.ENVIADO, via=ev.TEXTO, send_id=uuid.uuid4(), por_respuesta=True),
        "habilitada": True, "activa": False,
    }
    entorno["mapa"] = MAPA
    entorno["opciones"] = estado

    async def _enviar_opciones(conversation_id, clave, **kwargs):
        guarda = kwargs.pop("antes_de_enviar", None)
        estado["guarda"] = guarda  # solo los canales de texto la piden
        estado["llamadas"].append((conversation_id, clave, kwargs))
        if guarda is not None and (motivo := await guarda()):  # como el envío real: después de la idempotencia
            return ev.ResultadoEnvio(False, ev.GUARDA, motivo=motivo)
        return estado["resultado"]

    async def _habilitada(_tenant):
        return estado["habilitada"]

    async def _activa(_conv, _tenant):
        return estado["activa"]

    monkeypatch.setattr("core.gateway.opciones_envio.enviar_opciones", _enviar_opciones)
    monkeypatch.setattr(puente, "_opciones_habilitadas", _habilitada)
    monkeypatch.setattr(puente, "_conversacion_activa", _activa)
    return estado


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canal", "proveedor", "ventana"),
    [("web", "", None), ("whatsapp", "waweb", None), ("messenger", "", 2), ("instagram", "", 2), ("whatsapp", "meta", 3)],
    ids=["web", "qr", "messenger", "instagram", "meta"],
)
async def test_el_aviso_sale_como_plantilla_con_opciones_y_no_manda_ademas_el_texto(opciones, entorno, canal, proveedor, ventana) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    entorno["proveedor"] = proveedor
    conversacion = _conversacion(canal, ventana_horas=ventana)
    r = await _enviar(entorno, conversacion, key="k:op")
    assert (r.modo, r.detalle) == ("opciones", "texto")
    [(conv_id, clave, kwargs)] = opciones["llamadas"]
    assert (conv_id, clave) == (conversacion.id, CLAVE)
    assert kwargs == {
        "datos": {"cita.titulo": "Corte", "cita.fecha": "2026-11-02", "cita.hora": "10:00"},
        "clave_idempotencia": "k:op", "actor": f"evento:{EVENTO}", "sin_mensaje_de_fallo": True,
    }
    assert (opciones["guarda"] is not None) == (canal != "whatsapp" or proveedor == "waweb")  # guarda solo en texto
    assert entorno["textos"] == [] and entorno["plantillas"] == [] and entorno["sin_plantilla"] == []


@pytest.mark.asyncio
async def test_con_el_envio_de_opciones_apagado_ni_se_intenta_ni_se_deja_rastro(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    opciones["habilitada"] = False
    r = await _enviar(entorno, _conversacion("web"))
    assert opciones["llamadas"] == [] and r.modo == "texto" and entorno["textos"][0][0] == "Texto web Ana"


@pytest.mark.asyncio
async def test_sin_option_template_el_comportamiento_es_el_de_d2(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    entorno["mapa"] = {EVENTO: {"fallback_text_by_channel": {ac.WEB: "Texto web {cliente.nombre}"}}}
    r = await _enviar(entorno, _conversacion("web"))
    assert opciones["llamadas"] == [] and r.modo == "texto"


@pytest.mark.asyncio
async def test_los_eventos_de_cobranza_ignoran_un_option_template_guardado_a_mano(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    for evento in sorted(COBRANZA):
        entorno["mapa"] = {evento: {"option_template": CLAVE, "fallback_text_by_channel": {ac.WEB: "Debes {cargo.monto}"}}}
        r = await _enviar(entorno, _conversacion("web"), evento=evento, key=f"k:{evento}")
        assert r.modo == "texto" and entorno["textos"][-1][0].startswith("Debes")  # lo de D2, nunca el puente
    assert opciones["llamadas"] == []


@pytest.mark.asyncio
async def test_canales_sin_puente_siguen_su_camino(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    entorno["proveedor"] = "twilio"
    r = await _enviar(entorno, _conversacion("whatsapp", ventana_horas=3), key="k:t")
    assert opciones["llamadas"] == [] and r.modo != "opciones"


@pytest.mark.asyncio
async def test_qr_respeta_la_politica_antibaneo_antes_de_intentar(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    entorno["proveedor"] = "waweb"
    entorno["bloqueo"] = ac.SOLO_RESPUESTA
    r = await _enviar(entorno, _conversacion("whatsapp"))
    assert opciones["llamadas"] == []
    assert r.modo != "opciones"  # cae al camino de D2 (aquí sin texto de QR: el de siempre)


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["messenger", "instagram"])
async def test_messenger_e_instagram_sin_ventana_no_intentan_el_menu(opciones, entorno, canal) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    r = await _enviar(entorno, _conversacion(canal, ventana_horas=-1))
    assert opciones["llamadas"] == [] and r.modo != "opciones"


@pytest.mark.asyncio
async def test_con_la_conversacion_activa_no_se_manda_un_menu_de_texto_que_se_comeria_el_proximo_1(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    opciones["activa"] = True
    r = await _enviar(entorno, _conversacion("web"))
    assert opciones["guarda"] is not None  # la guarda corre dentro del envío (después de la idempotencia)
    assert (r.modo, entorno["textos"][0][0]) == ("texto", "Texto web Ana")  # cae al texto de D2


@pytest.mark.asyncio
async def test_whatsapp_meta_no_mira_la_actividad_porque_tiene_botones(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    opciones["activa"] = True
    entorno["proveedor"] = "meta"
    r = await _enviar(entorno, _conversacion("whatsapp", ventana_horas=3))
    assert r.modo == "opciones" and len(opciones["llamadas"]) == 1


# ---------------------------------------------------------------- si no sale
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [ev.NO_PUBLICADA, ev.NO_ENCONTRADA, ev.CONTEXTO_INCOMPLETO, ev.SIN_PLANTILLA, ev.BANDERA_APAGADA, ev.NO_ENCOLADO, ev.PAUSADA, ev.DATOS_INVALIDOS],
)
async def test_si_el_envio_no_sale_cae_al_texto_de_d2_una_sola_vez(opciones, entorno, outcome) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    opciones["resultado"] = ev.ResultadoEnvio(False, outcome, motivo="m")
    r = await _enviar(entorno, _conversacion("web"))
    assert (r.modo, r.detalle) == ("texto", ac.WEB) and len(opciones["llamadas"]) == 1
    assert [t for t, _ in entorno["textos"]] == ["Texto web Ana"]


@pytest.mark.asyncio
async def test_un_envio_ya_hecho_no_se_duplica_con_el_texto(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    opciones["resultado"] = ev.ResultadoEnvio(False, ev.DUPLICADO, send_id=uuid.uuid4())
    r = await _enviar(entorno, _conversacion("web"))
    assert r.modo == "duplicado" and entorno["textos"] == []


@pytest.mark.asyncio
async def test_un_resultado_ambiguo_se_reintenta_en_vez_de_caer_al_texto(opciones, entorno) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    opciones["resultado"] = ev.ResultadoEnvio(False, ev.ERROR_INTERNO, motivo="RuntimeError")
    with pytest.raises(notices.NoticeNotSentError):
        await _enviar(entorno, _conversacion("web"))
    assert entorno["textos"] == []


@pytest.mark.asyncio
async def test_un_fallo_inesperado_del_puente_no_pierde_el_aviso(opciones, entorno, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    async def _roto(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(puente, "_conversacion_activa", _roto)
    r = await _enviar(entorno, _conversacion("web"))
    assert (r.modo, r.detalle) == ("texto", ac.WEB)


# ---------------------------------------------------------------- regresión: banderas apagadas, los 8 eventos
@pytest.mark.asyncio
@pytest.mark.parametrize("evento", sorted(notices.EVENTOS))
async def test_regresion_con_option_template_mapeado_y_la_bandera_apagada_los_8_eventos_se_comportan_como_antes(  # type: ignore[no-untyped-def]
    opciones, entorno, monkeypatch, evento: str  # noqa: F811
) -> None:
    assert len(notices.EVENTOS) == 8
    plantilla = SimpleNamespace(
        id=uuid.uuid4(), name="aviso", language="es", category="UTILITY", parameter_format="POSITIONAL",
        components=[{"type": "BODY", "text": "Hola {{1}}"}],
    )

    async def _aprobada(_c, _n, _i):
        return plantilla

    monkeypatch.setattr(notices, "approved_template_for", _aprobada)
    entorno["mapa"] = {
        evento: {"template": "aviso", "language": "es", "variables": ["cliente.nombre"],
                 "fallback_text": "Aviso para {cliente.nombre}", "option_template": CLAVE}
    }

    entorno["con"](_conversacion("whatsapp", ventana_horas=2))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(uuid.uuid4(), evento, CONTEXTO_COMPLETO, notice_key=f"{evento}:a")
    assert r.modo == "texto" and entorno["textos"] == [("Aviso para Ana", {"client_message_id_prefix": f"{evento}:a"})]

    entorno["con"](_conversacion("whatsapp", ventana_horas=-5))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(uuid.uuid4(), evento, CONTEXTO_COMPLETO, notice_key=f"{evento}:b")
    assert r.modo == "plantilla" and len(entorno["plantillas"]) == 1

    # Un canal de texto sin la bandera de avisos: lo de siempre (ventana desconocida = cerrada → la plantilla de Meta);
    # el puente ni se mira.
    entorno["con"](_conversacion("web"))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(uuid.uuid4(), evento, CONTEXTO_COMPLETO, notice_key=f"{evento}:c")
    assert r.modo == "plantilla" and len(entorno["plantillas"]) == 2
    assert opciones["llamadas"] == [] and entorno["omitidos"] == []


# ---------------------------------------------------------------- el guardado
def test_conservar_la_conversacion_de_texto_con_puente_cuenta_como_contenido() -> None:
    assert ac._hay_puente(EVENTO, {"option_template": CLAVE}) is True
    assert ac._hay_puente(EVENTO, {}) is False
    assert ac._hay_puente("subscription.paused", {"option_template": CLAVE}) is False
