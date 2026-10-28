"""ADR-0178 D3 · aviso por evento → plantilla con opciones, contra Postgres y Redis reales.

Qué se prueba con la base y la cola reales (solo el `.delay` de Celery, las credenciales de Meta y la tool del journey
son el borde):

- **PUT** `/admin/settings/message-templates`: acepta una plantilla con opciones publicada y vigente del negocio; rechaza
  la de otro negocio, la borrador, la retirada, la borrada, la inexistente, la que cita datos que el evento no tiene y
  cualquier plantilla para un evento de cobranza; «omitido = conservar» frente a un panel viejo; `""` la borra.
- **Salida** del aviso de una cita: Meta con ventana abierta (botones), Meta con ventana cerrada (plantilla `_v<n>`
  aprobada), QR y chat web (menú numerado), con las variables del evento y SIN el texto de respaldo además.
- **Fallback a D2/legacy**: sin plantilla de Meta aprobada, plantilla retirada o de otro negocio, QR con la política en
  contra, conversación activa.
- **Banderas apagadas** (`avisos_por_canal`, `opciones_directas`): el comportamiento es el de siempre y no queda ni un
  `option_send` ni una fila de auditoría.
- **Cobranza**: aunque el mapa se guarde a mano, nunca sale como plantilla con opciones.
- **Journey**: evento → menú en el chat web → el cliente contesta «1» → el consumer ejecuta la opción sin LLM
  (D3 es el primer emisor fuera de turno de C2).

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

# ruff: noqa: F811
from __future__ import annotations

import os
import uuid
from datetime import timedelta
from typing import Any

import pytest
from _api_helpers import api_headers
from test_opciones_envio import _sql as _sql_envio
from test_opciones_envio import (  # noqa: F401  (negocios A, B y apagado; cola; ayudas del envío)
    cola,
    componentes_meta,
    conversacion,
    cuenta_meta,
    definicion,
    mensajes,
    negocios,
    publicar,
)
from test_opciones_menu_texto import (  # noqa: F401  (journey)
    WEB,
    _abrir,
    _activaciones,
    _recibir,
    cada_tenant_con_la_bandera_encendida,
)
from test_opciones_toque_entorno import (
    _bandera as _bandera_opciones,
)
from test_opciones_toque_entorno import (  # noqa: F401  (fixtures del journey)
    _binding,
    herramienta,
    legacy,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

EVENTO = "appointment.reminder"
MAP = "/api/v1/admin/settings/message-templates"
BODY = "Hola {cliente.nombre}, tu cita {cita.titulo} es el {cita.fecha}."


def _definicion(n: int = 3) -> dict[str, Any]:
    d = definicion(n, body=BODY)
    d["variables"] = ["cliente.nombre", "cita.titulo", "cita.fecha"]
    return d


def _payload_cita(**extra: Any) -> dict[str, Any]:
    return {
        "customer_id": None, "appointment_id": str(uuid.uuid4()), "horas": 24, "titulo": "Corte",
        "fecha": "2026-11-02", "hora": "10:00", "zona": "America/Bogota", **extra,
    }


# ------------------------------------------------------------------ ayudas
@pytest.fixture
def guarda_real() -> None:
    """Marca la prueba que necesita la guarda de actividad de verdad (el resto la desactiva)."""


@pytest.fixture(autouse=True)
def _sin_actividad_reciente(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Por defecto la conversación recién creada no cuenta como activa; la prueba de la guarda usa la real."""
    if "guarda_real" not in request.fixturenames:
        monkeypatch.setattr("core.gateway.avisos_opciones.VENTANA_DE_ACTIVIDAD", timedelta(seconds=-5))


async def _avisos(tid: uuid.UUID, encendida: bool) -> None:
    from core.gateway import avisos_bandera
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await avisos_bandera.fijar(tid, encendida, actor="test")


async def _opciones(tid: uuid.UUID, encendida: bool) -> None:
    await _bandera_opciones(tid, encendida)


async def _mapear(tid: uuid.UUID, evento: str, **entrada: Any) -> None:
    """Guarda el mapa tal cual, sin pasar por la API (para casos que la API rechaza o ya no cumplen)."""
    from sqlalchemy import select as sel

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    entrada = {"template": "", "language": "es", "variables": [], "fallback_text": "", **entrada}
    async with tenant_scope(tid):
        async with get_session() as s:
            fila = (await s.execute(sel(AppSettings).where(AppSettings.tenant_id == tid, AppSettings.deleted_at.is_(None)))).scalar_one_or_none()
            if fila is None:
                fila = AppSettings(tenant_id=tid)
                s.add(fila)
            fila.message_templates_map = {evento: entrada}


async def _cliente_de(tid: uuid.UUID, conv: uuid.UUID) -> uuid.UUID:
    [(c,)] = await _sql_envio(tid, "select customer_id from conversations where id = :c", c=conv)
    return c


async def _avisar(tid: uuid.UUID, cliente: uuid.UUID, payload: dict[str, Any] | None = None, topic: str = EVENTO) -> None:
    import core.events.core_handlers as handlers
    from core.shared.tenant import tenant_scope

    datos = {**_payload_cita(), **(payload or {}), "customer_id": str(cliente)}
    async with tenant_scope(tid):
        await handlers._avisar_suscripcion(topic, datos)


async def _enviado(tid: uuid.UUID, conv: uuid.UUID) -> list[dict[str, Any]]:
    return await mensajes(tid, conv)


async def _rastros(tid: uuid.UUID) -> tuple[int, int]:
    [(envios,)] = await _sql_envio(tid, "select count(*) from option_sends")
    [(auditoria,)] = await _sql_envio(tid, "select count(*) from audit_log where action = 'option_send'")
    return envios, auditoria


def _como(monkeypatch: pytest.MonkeyPatch, proveedor: str) -> None:
    async def credenciales(*_a: Any, **_k: Any) -> tuple[str, dict[str, str]]:
        return proveedor, ({"phone_number_id": "ph-test", "access_token": "t"} if proveedor == "meta" else {})

    monkeypatch.setattr("core.gateway.sender.resolve_credentials", credenciales)


async def _conexion_waweb(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            c = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="waweb", type_id="whatsapp_waweb", category="communication",
                label="QR", external_id=f"waweb:{uuid.uuid4().hex}",
            )
            s.add(c)
            await s.flush()
            return c.id


async def _permitir_qr(tid: uuid.UUID, modo: str = "allow_outbound") -> None:
    from sqlalchemy import select as sel

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            fila = (await s.execute(sel(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one_or_none()
            if fila is None:
                fila = AppSettings(tenant_id=tid)
                s.add(fila)
            fila.behavior_config = {**(fila.behavior_config or {}), "waweb_mode": modo}


@pytest.fixture
async def listo(negocios, cola):  # type: ignore[no-untyped-def]
    """A y B con la bandera de opciones encendida y la de avisos por canal encendida en A; limpia al terminar."""
    a, b, apagado = negocios
    await _avisos(a.tid, True)
    yield a, b, apagado
    await _avisos(a.tid, False)
    await _mapear(a.tid, EVENTO)  # sin puente para la siguiente prueba


# ------------------------------------------------------------------ PUT
async def test_el_mapa_guarda_una_plantilla_publicada_la_conserva_si_se_omite_y_se_borra_con_vacio(app_client, listo) -> None:
    a, _, _ = listo
    h = await api_headers(app_client, a.tid)
    clave = await publicar(a.tid, _definicion())

    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"option_template": clave}}})
    assert r.status_code == 200, r.text
    assert r.json()["mapping"][EVENTO]["option_template"] == clave

    # Un panel anterior guarda otra cosa (texto general) sin mandar el campo: el puente no se pierde.
    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"fallback_text": "Hola"}}})
    assert r.json()["mapping"][EVENTO]["option_template"] == clave
    # ... ni cuando el panel anterior ni siquiera manda el evento.
    r = await app_client.put(MAP, headers=h, json={"mapping": {}})
    assert r.json()["mapping"][EVENTO]["option_template"] == clave
    # ... ni cuando manda el evento con todo vacío.
    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {}}})
    assert r.json()["mapping"][EVENTO]["option_template"] == clave

    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"option_template": ""}}})
    assert r.status_code == 200 and r.json()["mapping"].get(EVENTO, {}).get("option_template", "") == ""


async def test_el_mapa_rechaza_lo_que_no_es_una_plantilla_con_opciones_vigente_de_este_negocio(app_client, listo) -> None:
    a, b, _ = listo
    h = await api_headers(app_client, a.tid)
    ajena = await publicar(b.tid, _definicion())
    borrador = await publicar(a.tid, _definicion(), estado="draft")
    retirada = await publicar(a.tid, _definicion(), version_retirada=True)
    borrada = await publicar(a.tid, _definicion(), borrada=True)
    cita_cobro = _definicion()
    cita_cobro["body"] = "Debes {cargo.monto}"
    cita_cobro["variables"] = ["cargo.monto"]
    con_cobro = await publicar(a.tid, cita_cobro)
    buena = await publicar(a.tid, _definicion())

    for clave, esperado in (
        (ajena, "No existe"), ("inexistente", "No existe"), (borrador, "no está publicada"),
        (retirada, "retirada"), (borrada, "No existe"), ("Con Espacios", "no es válida"),
    ):
        r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"option_template": clave}}})
        assert r.status_code == 400 and esperado in r.text, (clave, r.text)
    # La plantilla pide un dato que este evento no tiene (cobranza): el lint la aceptaría, el aviso caería en silencio.
    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"option_template": con_cobro}}})
    assert r.status_code == 400 and "cargo.monto" in r.text
    # Cobranza: ninguna plantilla con opciones, ni la buena.
    for evento in ("subscription.charge_created", "subscription.charge_reminder", "subscription.paused"):
        r = await app_client.put(MAP, headers=h, json={"mapping": {evento: {"option_template": buena}}})
        assert r.status_code == 400 and "cobranza" in r.text, evento
    # La invitación del comprador tampoco: negocio/cliente del evento son los del comprador, no los de la conversación.
    r = await app_client.put(MAP, headers=h, json={"mapping": {"provisioning.invitation": {"option_template": buena}}})
    assert r.status_code == 400 and "comprador" in r.text
    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"option_template": buena}}})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ salida
async def test_meta_con_ventana_abierta_sale_con_botones_y_las_variables_del_evento(listo, cola) -> None:
    a, _, _ = listo
    cuenta, _ids = await cuenta_meta(a.tid, {})
    clave = await publicar(a.tid, _definicion(3))
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text="Texto de respaldo")
    conv = await conversacion(a.tid, ventana=timedelta(hours=3), meta=cuenta)
    cliente = await _cliente_de(a.tid, conv)

    payload = _payload_cita()
    await _avisar(a.tid, cliente, payload)
    await _avisar(a.tid, cliente, payload)  # el outbox reintenta: no se duplica

    [msg] = await _enviado(a.tid, conv)
    assert msg["content"] == "Hola Ana, tu cita Corte es el 2026-11-02." and msg["status"] == "queued"
    assert msg["interactive"]["type"] == "button" and len(msg["interactive"]["buttons"]) == 3
    [(_, via, contexto, _, _)] = await _sql_envio(a.tid, "select id, via, context, expires_at, created_at from option_sends where conversation_id = :c", c=conv)
    assert via == "interactive"
    assert contexto == {"cliente.nombre": "Ana", "cita.titulo": "Corte", "cita.fecha": "2026-11-02"}
    assert len(cola) == 1


async def test_meta_con_ventana_cerrada_usa_la_plantilla_aprobada_y_sin_ella_cae_a_lo_de_siempre(listo, cola) -> None:
    a, _, _ = listo
    d = _definicion(2)
    cuenta, ids = await cuenta_meta(a.tid, {"cita_v1": ("APPROVED", componentes_meta(d))})
    con_plantilla = await publicar(a.tid, d, message_template_id=ids["cita_v1"])
    sin_plantilla = await publicar(a.tid, d)

    await _mapear(a.tid, EVENTO, option_template=con_plantilla)
    conv = await conversacion(a.tid, ventana=timedelta(hours=-5), meta=cuenta)
    await _avisar(a.tid, await _cliente_de(a.tid, conv))
    [msg] = await _enviado(a.tid, conv)
    assert (msg["template_name"], msg["status"]) == ("cita_v1", "queued")
    assert msg["components"][0]["parameters"][0]["text"] == "Ana"

    # Sin contraparte de Meta aprobada: no queda una alerta de «opciones»; sigue lo de siempre (`sin_plantilla` del
    # evento, que es lo que ve hoy el negocio).
    await _mapear(a.tid, EVENTO, option_template=sin_plantilla)
    conv2 = await conversacion(a.tid, ventana=timedelta(hours=-5), meta=cuenta)
    await _avisar(a.tid, await _cliente_de(a.tid, conv2))
    [fallo] = await _enviado(a.tid, conv2)
    assert (fallo["status"], fallo["error_reason"]) == ("failed", "sin_plantilla")
    assert "opciones:" not in fallo["content"] and not fallo["client_message_id"].startswith("opt_send:")


async def test_el_chat_web_recibe_el_menu_numerado_y_no_el_texto_ademas(listo, cola) -> None:
    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(3))
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text_by_channel={"webchat": "Texto web"})
    conv = await conversacion(a.tid, canal="web", ventana=None)
    await _avisar(a.tid, await _cliente_de(a.tid, conv))

    [msg] = await _enviado(a.tid, conv)
    assert msg["content"].startswith("Hola Ana, tu cita Corte es el 2026-11-02.") and msg["content"].endswith("1. Opción 1\n2. Opción 2\n3. Opción 3")
    assert msg["interactive"] is None and msg["client_message_id"].startswith("opt_send:")
    [(via,)] = await _sql_envio(a.tid, "select via from option_sends where conversation_id = :c", c=conv)
    assert via == "text"


async def test_qr_exige_la_politica_y_luego_envia_el_menu(listo, cola, monkeypatch) -> None:
    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(2))
    await _mapear(a.tid, EVENTO, option_template=clave)
    conexion = await _conexion_waweb(a.tid)
    conv = await conversacion(a.tid, ventana=None)
    await _sql_envio(a.tid, "update conversations set connection_id = :k where id = :c", k=conexion, c=conv)
    _como(monkeypatch, "waweb")
    cliente = await _cliente_de(a.tid, conv)

    await _permitir_qr(a.tid, "reply_only")  # un aviso no es una respuesta
    await _avisar(a.tid, cliente)
    assert [m for m in await _enviado(a.tid, conv) if m["client_message_id"] and m["client_message_id"].startswith("opt_send:")] == []

    await _permitir_qr(a.tid)
    await _avisar(a.tid, cliente, _payload_cita())
    enviados = [m for m in await _enviado(a.tid, conv) if (m["client_message_id"] or "").startswith("opt_send:")]
    assert len(enviados) == 1 and "1. Opción 1" in enviados[0]["content"]


# ------------------------------------------------------------------ banderas y fallback
async def test_con_cualquiera_de_las_dos_banderas_apagada_todo_es_como_antes(listo, cola) -> None:
    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(2))
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text="Texto de respaldo", fallback_text_by_channel={"webchat": "Texto web"})
    antes = await _rastros(a.tid)

    # Avisos por canal apagada: el chat web no recibe nada; WhatsApp con ventana abierta, el texto de siempre.
    await _avisos(a.tid, False)
    web = await conversacion(a.tid, canal="web", ventana=None)
    await _avisar(a.tid, await _cliente_de(a.tid, web))
    assert await _enviado(a.tid, web) == []
    cuenta, _ids = await cuenta_meta(a.tid, {})
    wa = await conversacion(a.tid, ventana=timedelta(hours=3), meta=cuenta)
    await _avisar(a.tid, await _cliente_de(a.tid, wa))
    [msg] = await _enviado(a.tid, wa)
    assert msg["content"] == "Texto de respaldo" and msg["interactive"] is None
    assert await _rastros(a.tid) == antes

    # Opciones directas apagada con avisos por canal encendida: el aviso sale como texto (D2) y no queda rastro de opciones.
    await _avisos(a.tid, True)
    await _opciones(a.tid, False)
    try:
        web2 = await conversacion(a.tid, canal="web", ventana=None)
        await _avisar(a.tid, await _cliente_de(a.tid, web2))
        [msg2] = await _enviado(a.tid, web2)
        assert msg2["content"] == "Texto web" and not msg2["client_message_id"].startswith("opt_send:")
        assert await _rastros(a.tid) == antes
    finally:
        await _opciones(a.tid, True)


@pytest.mark.parametrize("caso", ["retirada", "borrador", "otro_negocio", "inexistente"])
async def test_una_plantilla_que_ya_no_sirve_cae_al_texto_y_nunca_toca_a_otro_negocio(listo, cola, caso) -> None:
    a, b, _ = listo
    clave = {
        "retirada": lambda: publicar(a.tid, _definicion(), version_retirada=True),
        "borrador": lambda: publicar(a.tid, _definicion(), estado="draft"),
        "otro_negocio": lambda: publicar(b.tid, _definicion()),  # mapa guardado a mano apuntando al negocio B
        "inexistente": lambda: _async("no_existe"),
    }[caso]
    clave = await clave()
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text_by_channel={"webchat": "Texto web"})
    conv = await conversacion(a.tid, canal="web", ventana=None)
    envios_b = await _rastros(b.tid)

    await _avisar(a.tid, await _cliente_de(a.tid, conv))

    [msg] = await _enviado(a.tid, conv)
    assert msg["content"] == "Texto web" and not msg["client_message_id"].startswith("opt_send:")
    assert await _rastros(b.tid) == envios_b, "el negocio B no recibe ni un envío"


async def _async(valor: Any) -> Any:
    return valor


async def test_la_cobranza_nunca_sale_como_plantilla_con_opciones_aunque_el_mapa_se_guarde_a_mano(listo, cola) -> None:
    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(2))
    for evento in ("subscription.charge_created", "subscription.charge_reminder", "subscription.paused"):
        await _mapear(a.tid, evento, option_template=clave, fallback_text_by_channel={"webchat": "Debes {cargo.monto}"})
        conv = await conversacion(a.tid, canal="web", ventana=None)
        await _avisar(
            a.tid, await _cliente_de(a.tid, conv), {"monto": "1000", "moneda": "COP", "dias": -3, "charge_id": str(uuid.uuid4())}, topic=evento,
        )
        for m in await _enviado(a.tid, conv):
            assert not (m["client_message_id"] or "").startswith("opt_send:"), evento
    [(envios,)] = await _sql_envio(a.tid, "select count(*) from option_sends where option_template_id in (select id from option_templates where key = :k)", k=clave)
    assert envios == 0


async def test_con_la_conversacion_activa_el_menu_de_texto_no_sale_y_cae_al_texto(listo, cola, guarda_real) -> None:
    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(2))
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text_by_channel={"webchat": "Texto web"})
    conv = await conversacion(a.tid, canal="web", ventana=None)
    cliente = await _cliente_de(a.tid, conv)
    await _sql_envio(
        a.tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, status) "
        "values (:i, :t, :c, 'inbound', 'customer', '1', 'delivered')",
        i=uuid.uuid4(), t=a.tid, c=conv,
    )
    await _avisar(a.tid, cliente)
    salientes = [m for m in await _enviado(a.tid, conv)]
    assert [m["content"] for m in salientes] == ["Texto web"]


async def test_el_reintento_del_outbox_en_un_canal_de_texto_no_duplica_aunque_el_menu_cuente_como_actividad(
    listo, cola, guarda_real
) -> None:
    """El menú de texto recién enviado es actividad en la conversación: la guarda NO puede correr antes de la
    idempotencia, o el reintento caería al texto de D2 y el cliente recibiría el aviso dos veces."""
    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(2))
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text_by_channel={"webchat": "Texto web"})
    conv = await conversacion(a.tid, canal="web", ventana=None)
    cliente = await _cliente_de(a.tid, conv)
    payload = _payload_cita()

    await _avisar(a.tid, cliente, payload)
    await _avisar(a.tid, cliente, payload)  # reintento del mismo aviso

    [msg] = await _enviado(a.tid, conv)
    assert msg["client_message_id"].startswith("opt_send:"), "el único saliente es el menú, no el texto de D2"


async def test_la_eleccion_de_conversacion_de_texto_es_la_de_d2_si_opciones_directas_esta_apagada(listo, cola) -> None:
    """Un `option_template` guardado no cambia a qué conversación de texto va el aviso mientras la bandera de
    opciones directas esté apagada (D2 exacto: la que tiene texto definido, no la más reciente)."""
    from core.gateway import avisos_canal
    from core.shared.tenant import tenant_scope

    a, _, _ = listo
    clave = await publicar(a.tid, _definicion(2))
    await _mapear(a.tid, EVENTO, option_template=clave, fallback_text_by_channel={"messenger_meta": "Texto Messenger"})
    messenger = await conversacion(a.tid, canal="messenger", ventana=timedelta(hours=2))
    cliente = await _cliente_de(a.tid, messenger)
    from test_opciones_envio import _scoped

    from core.db.models import Conversation

    async def _web(sesion: Any) -> uuid.UUID:  # la más reciente, del mismo cliente y sin texto definido para ella
        conv = Conversation(tenant_id=a.tid, customer_id=cliente, channel="web")
        sesion.add(conv)
        await sesion.flush()
        return conv.id

    web = await _scoped(a.tid, _web)
    for encendida, esperada in ((False, messenger), (True, web)):
        await _opciones(a.tid, encendida)
        try:
            async with tenant_scope(a.tid):
                elegida = await avisos_canal.conversacion_de_texto_para(cliente, EVENTO)
            assert elegida is not None and elegida.id == esperada, encendida
        finally:
            await _opciones(a.tid, True)


# ------------------------------------------------------------------ journey
async def test_journey_evento_menu_web_respuesta_1_ejecuta_la_opcion_sin_llm(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded, monkeypatch
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    monkeypatch.setattr("core.gateway.avisos_opciones.VENTANA_DE_ACTIVIDAD", timedelta(seconds=-5))
    await _binding(tid)
    await _opciones(tid, True)
    await _avisos(tid, True)
    try:
        clave = await publicar(tid, _definicion(3))
        await _mapear(tid, EVENTO, option_template=clave)
        conv, cliente_externo = await _abrir(tid, WEB)
        cliente = await _cliente_de(tid, conv)

        await _avisar(tid, cliente)  # 1) el evento sale como menú numerado, fuera de turno
        [(send_id, via)] = await _sql_envio(tid, "select id, via from option_sends where conversation_id = :c", c=conv)
        assert via == "text"

        recorded.clear()
        await _recibir(tid, cliente_externo, "1", WEB)  # 2) el cliente contesta «1»

        assert recorded == [], "cero invocaciones del LLM: el «1» no llegó al agente"
        assert [a for a, _ in herramienta.llamadas] == [{"pedido": "A-1"}]
        assert await _activaciones(tid, send_id) == [("o1", "done")]
    finally:
        await _avisos(tid, False)
        await _mapear(tid, EVENTO)
