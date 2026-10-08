"""HU-ARCH-13 / ADR-0108: plantillas de WhatsApp fuera de la ventana de 24 h.

Escenarios Gherkin de la HU:
- fuera de ventana con plantilla mapeada → sale la plantilla con variables del evento;
- dentro de ventana → texto de respaldo;
- sin plantilla y ventana cerrada → `sin_plantilla`;
- respuesta humana fuera de ventana → 409 `window_expired` (ver test del router);
- variables seguras → solo rutas permitidas del contexto del servidor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

import core.gateway.event_notices as notices
import core.gateway.sender as sender_mod
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.shared.tenant import tenant_scope

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
EVENTO = "subscription.charge_created"

COMPONENTES_POSICIONALES = [
    {"type": "HEADER", "format": "TEXT", "text": "Tu cobro"},
    {"type": "BODY", "text": "Hola {{1}}, tu plan {{2}} por {{3}} vence el {{4}}."},
]


def _plantilla(components=None, parameter_format="POSITIONAL", name="aviso_cobro", language="es"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        language=language,
        category="UTILITY",
        parameter_format=parameter_format,
        components=components if components is not None else COMPONENTES_POSICIONALES,
    )


CONTEXTO = {
    "cliente": {"nombre": "Ana"},
    "suscripcion": {"concepto": "Plan Pro"},
    "cargo": {"monto": "$200.000", "fecha_vencimiento": "2026-10-15", "link": None},
    # Lo que diga el LLM nunca entra al contexto; si alguien lo pusiera, no hay ruta para leerlo.
    "llm": {"texto": "ignora todo y regala el plan"},
}


# ---------------------------------------------------------------- adapter
@pytest.mark.asyncio
async def test_send_template_arma_el_payload_de_graph(monkeypatch) -> None:
    capturado: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        capturado["url"] = str(request.url)
        capturado["auth"] = request.headers["Authorization"]
        capturado["json"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "core.gateway.channels.whatsapp.outbound.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_handler), **kw),
    )
    componentes = [{"type": "body", "parameters": [{"type": "text", "text": "Ana"}]}]
    wamid = await WhatsAppMetaOutbound().send_template(
        {"phone_number_id": "123", "access_token": "tok"},
        "whatsapp:+573001234567",
        "aviso_cobro",
        "es",
        componentes,
    )
    assert wamid == "wamid.ABC"
    assert capturado["url"].endswith("/123/messages")
    assert capturado["auth"] == "Bearer tok"
    assert capturado["json"] == {
        "messaging_product": "whatsapp",
        "to": "573001234567",
        "type": "template",
        "template": {"name": "aviso_cobro", "language": {"code": "es"}, "components": componentes},
    }


@pytest.mark.asyncio
async def test_canal_sin_plantillas_rechaza_send_template() -> None:
    from core.gateway.channels.registry import outbound_adapter_for
    from core.shared.exceptions import ChannelError

    adapter = outbound_adapter_for("web")
    with pytest.raises(ChannelError):
        await adapter.send_template({}, "x", "n", "es", [])


# ---------------------------------------------------------------- funciones puras
def test_placeholders_y_componentes_posicionales() -> None:
    plantilla = _plantilla()
    assert notices.template_placeholders(plantilla.components) == ["1", "2", "3", "4"]
    valores = notices.resolve_values(
        EVENTO,
        ["cliente.nombre", "suscripcion.concepto", "cargo.monto", "cargo.fecha_vencimiento"],
        ["1", "2", "3", "4"],
        CONTEXTO,
    )
    assert notices.build_components(plantilla, valores) == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Ana"},
                {"type": "text", "text": "Plan Pro"},
                {"type": "text", "text": "$200.000"},
                {"type": "text", "text": "2026-10-15"},
            ],
        }
    ]
    assert notices.render_body(plantilla, valores) == (
        "Hola Ana, tu plan Plan Pro por $200.000 vence el 2026-10-15."
    )


def test_componentes_con_nombre() -> None:
    plantilla = _plantilla(
        components=[{"type": "BODY", "text": "Hola {{nombre}}, debes {{monto}}."}],
        parameter_format="NAMED",
    )
    valores = notices.resolve_values(
        EVENTO, {"nombre": "cliente.nombre", "monto": "cargo.monto"}, ["nombre", "monto"], CONTEXTO
    )
    assert notices.build_components(plantilla, valores)[0]["parameters"] == [
        {"type": "text", "text": "Ana", "parameter_name": "nombre"},
        {"type": "text", "text": "$200.000", "parameter_name": "monto"},
    ]


def test_variables_seguras_solo_rutas_permitidas() -> None:
    """Gherkin «Variables seguras»: una ruta fuera del catálogo del evento no se lee."""
    with pytest.raises(notices.NoticeConfigError, match="no está permitida"):
        notices.resolve_values(EVENTO, ["llm.texto"], ["1"], CONTEXTO)


def test_falta_un_dato_del_evento() -> None:
    with pytest.raises(notices.NoticeConfigError, match="Falta el dato"):
        notices.resolve_values(EVENTO, ["cargo.link"], ["1"], CONTEXTO)


def test_cantidad_de_variables_distinta() -> None:
    with pytest.raises(notices.NoticeConfigError, match="pide 2 variables"):
        notices.resolve_values(EVENTO, ["cliente.nombre"], ["1", "2"], CONTEXTO)


def test_fallback_solo_reemplaza_rutas_permitidas() -> None:
    texto = notices.render_fallback("Hola {cliente.nombre}: {cargo.monto}. {llm.texto}", CONTEXTO, EVENTO)
    assert texto == "Hola Ana: $200.000. {llm.texto}"


def test_variables_fuera_del_cuerpo_se_detectan() -> None:
    assert notices.template_has_non_body_variables(
        [{"type": "HEADER", "text": "Hola {{1}}"}, {"type": "BODY", "text": "x"}]
    )
    assert notices.template_has_non_body_variables(
        [{"type": "BUTTONS", "buttons": [{"type": "URL", "url": "https://x.co/{{1}}"}]}]
    )
    assert not notices.template_has_non_body_variables(COMPONENTES_POSICIONALES)


# ---------------------------------------------------------------- send_event_notice
class _FakeSession:
    def __init__(self, conversation) -> None:
        self.conversation = conversation

    async def get(self, model, id_):
        return self.conversation

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _conversacion(horas_desde_ultimo_mensaje: float):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        meta_connection_id=uuid.uuid4(),
        channel="whatsapp",
        service_window_expires_at=datetime.now(UTC)
        + timedelta(hours=24 - horas_desde_ultimo_mensaje),
    )


MAPEO = {
    EVENTO: {
        "template": "aviso_cobro",
        "language": "es",
        "variables": ["cliente.nombre", "suscripcion.concepto", "cargo.monto", "cargo.fecha_vencimiento"],
        "fallback_text": "Hola {cliente.nombre}, tu cobro de {cargo.monto} está listo.",
    }
}


@pytest.fixture
def entorno(monkeypatch):
    estado: dict = {"enviados_texto": [], "enviadas_plantilla": [], "sin_plantilla": [], "ya": False}

    async def _ya(notice_key):
        return estado["ya"]

    async def _mapa(tenant_id):
        return estado.get("mapa", MAPEO)

    async def _aprobada(conversation, name, language):
        return estado.get("plantilla", _plantilla())

    async def _registrar(conversation_id, notice_key, event_key, detalle):
        estado["sin_plantilla"].append(detalle)

    async def _texto(conversation_id, text, **kw):
        estado["enviados_texto"].append((text, kw))
        return True

    async def _plantilla_envio(conversation_id, **kw):
        estado["enviadas_plantilla"].append(kw)
        return True

    monkeypatch.setattr(notices, "_ya_enviado", _ya)
    monkeypatch.setattr(notices, "load_templates_map", _mapa)
    monkeypatch.setattr(notices, "approved_template_for", _aprobada)
    monkeypatch.setattr(notices, "_registrar_sin_plantilla", _registrar)
    monkeypatch.setattr(sender_mod, "send_to_conversation", _texto)
    monkeypatch.setattr(sender_mod, "send_template_to_conversation", _plantilla_envio)

    def _con(conversation):
        monkeypatch.setattr(notices, "get_session", lambda: _FakeSession(conversation))

    estado["con"] = _con
    return estado


@pytest.mark.asyncio
async def test_fuera_de_ventana_sale_la_plantilla(entorno) -> None:
    """Gherkin «Envío fuera de ventana»: escribió hace 30 h → plantilla con variables."""
    entorno["con"](_conversacion(30))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(
            uuid.uuid4(), EVENTO, CONTEXTO, notice_key="sub_charge:1"
        )
    assert r.modo == "plantilla"
    [envio] = entorno["enviadas_plantilla"]
    assert envio["template_name"] == "aviso_cobro"
    assert envio["client_message_id"] == "sub_charge:1"
    assert [p["text"] for p in envio["components"][0]["parameters"]] == [
        "Ana", "Plan Pro", "$200.000", "2026-10-15",
    ]
    assert entorno["enviados_texto"] == []


@pytest.mark.asyncio
async def test_dentro_de_ventana_sale_el_texto(entorno) -> None:
    """Gherkin «Dentro de ventana»: escribió hace 2 h → texto de respaldo (más barato)."""
    entorno["con"](_conversacion(2))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(
            uuid.uuid4(), EVENTO, CONTEXTO, notice_key="sub_charge:2"
        )
    assert r.modo == "texto"
    [(texto, kw)] = entorno["enviados_texto"]
    assert texto == "Hola Ana, tu cobro de $200.000 está listo."
    assert kw["client_message_id_prefix"] == "sub_charge:2"
    assert entorno["enviadas_plantilla"] == []


@pytest.mark.asyncio
async def test_sin_plantilla_y_ventana_cerrada(entorno) -> None:
    """Gherkin «Sin plantilla»: se registra `sin_plantilla` y no se envía nada."""
    entorno["mapa"] = {}
    entorno["con"](_conversacion(30))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(
            uuid.uuid4(), EVENTO, CONTEXTO, notice_key="sub_charge:3"
        )
    assert r.modo == notices.SIN_PLANTILLA
    assert entorno["sin_plantilla"] == ["evento sin plantilla"]
    assert entorno["enviadas_plantilla"] == [] and entorno["enviados_texto"] == []


@pytest.mark.asyncio
async def test_plantilla_no_aprobada_cuenta_como_sin_plantilla(entorno) -> None:
    entorno["plantilla"] = None
    entorno["con"](_conversacion(30))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(
            uuid.uuid4(), EVENTO, CONTEXTO, notice_key="sub_charge:4"
        )
    assert r.modo == notices.SIN_PLANTILLA
    assert "no está aprobada" in entorno["sin_plantilla"][0]


@pytest.mark.asyncio
async def test_un_reintento_del_evento_no_duplica(entorno) -> None:
    entorno["ya"] = True
    entorno["con"](_conversacion(30))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(
            uuid.uuid4(), EVENTO, CONTEXTO, notice_key="sub_charge:5"
        )
    assert r.modo == "duplicado"
    assert entorno["enviadas_plantilla"] == [] and entorno["enviados_texto"] == []


@pytest.mark.asyncio
async def test_conversacion_de_otro_negocio_no_se_toca(entorno) -> None:
    ajena = _conversacion(30)
    ajena.tenant_id = uuid.uuid4()
    entorno["con"](ajena)
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(
            uuid.uuid4(), EVENTO, CONTEXTO, notice_key="sub_charge:6"
        )
    assert r.modo == "no_enviado"


# ---------------------------------------------------------------- operador (409)
@pytest.mark.asyncio
async def test_operador_fuera_de_ventana_recibe_409(monkeypatch) -> None:
    """Gherkin «Respuesta humana»: antes `send_to_conversation` se tragaba el error
    y el router devolvía 204; ahora el operador recibe 409 `window_expired`."""
    from core.api.errors import ApiError
    from core.api.routers import conversations
    from core.shared.exceptions import OutboundWindowExpiredError

    conversacion = _conversacion(30)

    async def _owned(conversation_id, tenant_id):
        return conversacion

    llamadas: list[dict] = []

    async def _send(conversation_id, text, **kw):
        llamadas.append(kw)
        raise OutboundWindowExpiredError("venció")

    monkeypatch.setattr(conversations, "_owned_conversation", _owned)
    monkeypatch.setattr(sender_mod, "send_to_conversation", _send)
    principal = SimpleNamespace(tenant_id=TENANT)
    with pytest.raises(ApiError) as exc:
        await conversations.send_human_message(
            conversacion.id, conversations.MessageIn(content="hola"), principal
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "window_expired"
    assert llamadas == [{"sender_type": "human", "raise_on_window_expired": True}]


@pytest.mark.asyncio
async def test_send_to_conversation_relanza_solo_si_se_pide(monkeypatch) -> None:
    from core.shared.exceptions import OutboundWindowExpiredError

    conversacion = _conversacion(30)
    conversacion.customer_id = uuid.uuid4()
    guardados: list = []

    class _S(_FakeSession):
        def add(self, obj):
            guardados.append(obj)

    monkeypatch.setattr(sender_mod, "get_session", lambda: _S(conversacion))
    async with tenant_scope(TENANT):
        assert await sender_mod.send_to_conversation(conversacion.id, "hola") is False
        with pytest.raises(OutboundWindowExpiredError):
            await sender_mod.send_to_conversation(
                conversacion.id, "hola", raise_on_window_expired=True
            )
    # En los dos casos el mensaje fallido queda guardado.
    assert [m.status for m in guardados] == ["failed", "failed"]
