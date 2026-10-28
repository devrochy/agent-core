"""SPEC-40 · pista C2: la respuesta de texto a un menú numerado, contra Postgres real (ADR-0172 adenda).

En un canal sin botones (QR/waweb, chat web, Messenger, Instagram) el envío de una plantilla con opciones sale como
texto numerado y el cliente contesta «1» o el título. El consumer lo resuelve ANTES del LLM, como si hubiera tocado el
botón. Qué se prueba aquí con la base y la cola reales (solo la tool y el LLM son el borde):

- **Journeys** QR simulado y chat web: envío (`send_options_to_conversation`) → «1» → tool ejecutada UNA vez →
  respuesta fija → cero invocaciones del LLM; la respuesta por título; el segundo «1» no re-ejecuta.
- **Una sola ejecución** con dos entregas simultáneas del mismo «1» y con la elección repetida sobre un menú que
  sigue siendo el último saliente (`option_activations.dedupe_key`).
- **Guardas**: bandera apagada, menú que no es el último saliente, menú vencido, canal sin `respuesta_numerica`,
  envío con botones, texto que no es una elección exacta, número sin menú → el mensaje sigue al agente.
- **Sensible**: una opción que pide confirmación NO se ejecuta por texto (texto fijo, sin activación, sin tool).
- **Seguridad**: el cliente solo manda texto (un payload firmado escrito a mano no ejecuta nada) y un menú de otro
  negocio no se alcanza.
- **Auditoría** con `via=text`.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text
from test_opciones_envio import (  # noqa: F401  (fixtures y ayudas del envío)
    cola,
    definicion,
    enviar,
    negocios,
    publicar,
)
from test_opciones_toque import HASH
from test_opciones_toque_entorno import (  # noqa: F401  (fixtures y ayudas del journey)
    _bandera,
    _binding,
    _inbound,
    _inmediato,
    _salientes,
    _sql,
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

QR = ("whatsapp", "waweb")
WEB = ("web", "web")
MESSENGER = ("messenger", "meta")
INSTAGRAM = ("instagram", "meta")
META = ("whatsapp", "meta")


# ---------------------------------------------------------------- ayudas
def _entrada(tid: uuid.UUID, cliente: str, contenido: str, canal: tuple[str, str], **extra: str) -> dict:
    d = _inbound(tid, cliente, contenido)
    d["channel"], d["provider"] = canal
    d.update(extra)
    return d


async def _recibir(tid: uuid.UUID, cliente: str, contenido: str, canal: tuple[str, str], **extra: str) -> None:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_entrada(tid, cliente, contenido, canal, **extra))


async def _abrir(tid: uuid.UUID, canal: tuple[str, str]) -> tuple[uuid.UUID, str]:
    """Una conversación abierta por el consumer (como la de un cliente real) en el canal y proveedor dados."""
    cliente = uniq("c2")
    await _recibir(tid, cliente, "hola", canal)
    [(conv,)] = await _sql(
        tid,
        "select cv.id from conversations cv join customers c on c.id = cv.customer_id where c.external_id = :e "
        "order by cv.started_at desc limit 1",
        e=cliente,
    )
    return conv, cliente


def _como_waweb(monkeypatch: pytest.MonkeyPatch) -> None:
    """El proveedor de la conversación para el envío (la ruta de la conversación es de Meta en el stub de `cola`)."""

    async def waweb(*_a: Any, **_k: Any) -> tuple[str, dict[str, str]]:
        return "waweb", {}

    monkeypatch.setattr("core.gateway.sender.resolve_credentials", waweb)


async def _activaciones(tid: uuid.UUID, send_id: uuid.UUID) -> list[tuple[str, str]]:
    return [(f[0], f[1]) for f in await _sql(tid, "select option_id, state from option_activations where send_id = :s", s=send_id)]


async def _auditoria(tid: uuid.UUID, conv: uuid.UUID) -> list[Any]:
    return await _sql(tid, "select input, output from audit_log where action = 'option_tap' and conversation_id = :c", c=conv)


async def _menu(tid: uuid.UUID, conv: uuid.UUID, *, n: int = 3, **kwargs: Any):
    clave = await publicar(tid, definicion(n))
    r = await enviar(tid, conv, clave, **kwargs)
    assert r.enviado and r.via == "text", f"el envío debía salir como texto numerado: {r}"
    return r


async def crear_menu_de_texto(
    tid: uuid.UUID, conv: uuid.UUID, *, via: str = "text", vence_en: timedelta = timedelta(hours=24), con_mensaje: bool = True,
    definicion_: dict | None = None,
) -> uuid.UUID:
    """Un `option_sends` y (opcional) su mensaje saliente escritos a mano, para fijar la vía o el vencimiento que el
    envío real no deja elegir. Devuelve el `send_id`."""
    from core.db.models import Message

    clave = f"t{uuid.uuid4().hex[:10]}"
    template_id, version_id, send_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _f(s: Any) -> None:
        await s.execute(
            text("insert into option_templates (id, tenant_id, key, name, status, current_version_n) values (:i, :t, :k, 'x', 'published', 1)"),
            {"i": template_id, "t": tid, "k": clave},
        )
        await s.execute(
            text(
                "insert into option_template_versions (id, tenant_id, option_template_id, version_n, definition, definition_hash) "
                "values (:i, :t, :tp, 1, cast(:d as jsonb), :h)"
            ),
            {"i": version_id, "t": tid, "tp": template_id, "d": json.dumps(definicion_ or definicion(3)), "h": HASH},
        )
        await s.execute(
            text(
                "insert into option_sends (id, tenant_id, option_template_id, version_id, conversation_id, via, context, expires_at) "
                "values (:i, :t, :tp, :v, :c, :via, cast(:ctx as jsonb), :e)"
            ),
            {"i": send_id, "t": tid, "tp": template_id, "v": version_id, "c": conv, "via": via,
             "ctx": json.dumps({"cliente.nombre": "Ana"}), "e": datetime.now(UTC) + vence_en},
        )
        if con_mensaje:
            s.add(
                Message(
                    tenant_id=tid, conversation_id=conv, direction="outbound", sender_type="agent",
                    content="Hola\n\n1. Opción 1\n2. Opción 2\n3. Opción 3", status="sent", client_message_id=f"opt_send:{send_id}:0",
                )
            )

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            await _f(s)
    return send_id


@pytest.fixture
def cada_tenant_con_la_bandera_encendida(legacy, monkeypatch):  # type: ignore[no-untyped-def]  # noqa: F811
    """El negocio del módulo con la bandera encendida y el binding de la tool habilitado y sin confirmación."""
    _inmediato(monkeypatch)
    return legacy[0]


# ---------------------------------------------------------------- journeys: QR y chat web
@pytest.mark.parametrize("canal", [QR, WEB, MESSENGER, INSTAGRAM], ids=["qr", "web", "messenger", "instagram"])
async def test_journey_menu_numerado_respuesta_1_ejecuta_una_vez_con_respuesta_fija_y_el_llm_en_cero(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, monkeypatch, recorded, canal  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    if canal == QR:
        _como_waweb(monkeypatch)
    conv, cliente = await _abrir(tid, canal)

    # 1) El envío sale como texto numerado, sin botón ni payload, y crea el `option_sends` de vía `text`.
    r = await _menu(tid, conv)
    [(contenido, interactivo)] = await _sql(
        tid, "select content, interactive from messages where conversation_id = :c and client_message_id like 'opt_send:%'", c=conv
    )
    assert contenido.endswith("1. Opción 1\n2. Opción 2\n3. Opción 3") and interactivo is None and "opt:" not in contenido

    # 2) El cliente contesta «1»: el consumer lo resuelve sin LLM y ejecuta la tool de la opción 1.
    recorded.clear()
    antes = len(await _salientes(tid, conv))
    await _recibir(tid, cliente, "1", canal)

    assert recorded == [], "cero invocaciones del LLM: el «1» no llegó al agente"
    assert [a for a, _ in herramienta.llamadas] == [{"pedido": "A-1"}]
    assert (await _salientes(tid, conv))[antes:] == ["Listo, Cliente."], "la respuesta fija de la opción, por la cola normal"
    assert await _activaciones(tid, r.send_id) == [("o1", "done")]
    [(entrada, salida)] = await _auditoria(tid, conv)
    assert entrada["via"] == "text" and entrada["tipo"] == "opt" and entrada["option_id"] == "o1"
    assert salida["outcome"] == "ok"
    [(id_respuesta, estado, remitente)] = await _sql(
        tid, "select id, status, sender_type from messages where conversation_id = :c order by created_at desc limit 1", c=conv
    )
    assert (estado, remitente) == ("queued", "agent"), "la respuesta sale por la cola de siempre (ahí rigen el delay y el tope del QR)"
    assert cola[-1] == str(id_respuesta), "y es lo último que se encoló"

    # 3) Un segundo «1»: el menú ya no es lo último que el cliente recibió, así que va al agente y no re-ejecuta.
    await _recibir(tid, cliente, "1", canal)
    assert len(herramienta.llamadas) == 1, "la tool corrió una sola vez"
    assert await _activaciones(tid, r.send_id) == [("o1", "done")]
    assert recorded == ["1"], "el «1» fuera de contexto sigue al agente como siempre"


async def test_la_respuesta_por_titulo_exacto_elige_la_opcion_sin_importar_mayusculas_ni_espacios(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    r = await _menu(tid, conv)
    recorded.clear()

    await _recibir(tid, cliente, "  OPCIÓN   2 ", WEB)

    assert recorded == [] and [a for a, _ in herramienta.llamadas] == [{"pedido": "A-2"}]
    assert await _activaciones(tid, r.send_id) == [("o2", "done")]


async def test_un_numero_fuera_de_rango_o_con_mas_texto_sigue_al_agente(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    r = await _menu(tid, conv)
    recorded.clear()

    for contenido in ("4", "1 por favor", "Opción"):
        await _recibir(tid, cliente, contenido, WEB)

    assert recorded == ["4", "1 por favor", "Opción"] and herramienta.llamadas == []
    assert await _activaciones(tid, r.send_id) == []


# ---------------------------------------------------------------- una sola ejecución
async def test_dos_entregas_simultaneas_del_mismo_1_ejecutan_la_tool_una_sola_vez(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    r = await _menu(tid, conv)

    await asyncio.gather(*(_recibir(tid, cliente, "1", WEB) for _ in range(3)))

    assert len(herramienta.llamadas) == 1, "el UNIQUE (tenant_id, dedupe_key) deja pasar a una sola entrega"
    assert await _activaciones(tid, r.send_id) == [("o1", "done")]


async def test_elegir_otra_vez_sobre_un_menu_que_sigue_siendo_el_ultimo_saliente_no_re_ejecuta(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, recorded  # noqa: F811
) -> None:
    """Si la respuesta fija no llegó a persistirse (la cola la rechazó), el menú sigue siendo el último saliente y el
    segundo «1» vuelve a llegar al resolver: la base lo frena (modo exclusivo: ni siquiera otra opción)."""
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    send_id = await crear_menu_de_texto(tid, conv)
    recorded.clear()

    async def sin_cola(*_a: Any, **_k: Any) -> bool:
        return False

    import core.gateway.sender as sender

    original = sender.send_to_conversation
    sender.send_to_conversation = sin_cola  # type: ignore[assignment]
    try:
        await _recibir(tid, cliente, "1", WEB)
        await _recibir(tid, cliente, "1", WEB)
        await _recibir(tid, cliente, "2", WEB)
    finally:
        sender.send_to_conversation = original  # type: ignore[assignment]

    assert recorded == [], "ninguna de las tres llegó al agente"
    assert len(herramienta.llamadas) == 1 and await _activaciones(tid, send_id) == [("o1", "done")]


# ---------------------------------------------------------------- guardas: todo lo demás sigue al agente
async def test_con_la_bandera_apagada_el_1_llega_al_agente_como_siempre(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    r = await _menu(tid, conv)
    await _bandera(tid, False)
    try:
        recorded.clear()
        await _recibir(tid, cliente, "1", WEB)
    finally:
        await _bandera(tid, True)

    assert recorded == ["1"] and herramienta.llamadas == [] and await _activaciones(tid, r.send_id) == []
    assert await _auditoria(tid, conv) == []


async def test_si_el_menu_no_es_el_ultimo_saliente_el_1_llega_al_agente(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    from core.gateway.sender import send_to_conversation
    from core.shared.tenant import tenant_scope

    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    r = await _menu(tid, conv)
    async with tenant_scope(tid):
        assert await send_to_conversation(conv, "¿Algo más en lo que te ayude?")  # el agente o un humano siguió hablando
    recorded.clear()

    await _recibir(tid, cliente, "1", WEB)

    assert recorded == ["1"] and herramienta.llamadas == [] and await _activaciones(tid, r.send_id) == []


async def test_un_menu_vencido_no_se_interpreta(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    send_id = await crear_menu_de_texto(tid, conv, vence_en=timedelta(minutes=-1))
    recorded.clear()

    await _recibir(tid, cliente, "1", WEB)

    assert recorded == ["1"], "a destiempo va al agente, no a la respuesta fija de «venció»"
    assert herramienta.llamadas == [] and await _activaciones(tid, send_id) == []


@pytest.mark.parametrize("via", ["interactive", "template"])
async def test_un_envio_con_botones_no_se_contesta_con_texto(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, recorded, via: str  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    send_id = await crear_menu_de_texto(tid, conv, via=via)
    recorded.clear()

    await _recibir(tid, cliente, "1", WEB)

    assert recorded == ["1"] and herramienta.llamadas == [] and await _activaciones(tid, send_id) == []


async def test_un_canal_sin_respuesta_numerica_no_interpreta_el_texto(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, recorded  # noqa: F811
) -> None:
    """WhatsApp Meta tiene botones: aunque su último saliente sea un menú de texto (interactivo degradado por los
    topes de Meta), contestarlo con «1» llega al agente."""
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, META)
    send_id = await crear_menu_de_texto(tid, conv)
    recorded.clear()

    await _recibir(tid, cliente, "1", META)

    assert recorded == ["1"] and herramienta.llamadas == [] and await _activaciones(tid, send_id) == []


async def test_un_numero_sin_ningun_menu_sigue_al_agente(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    _conv, cliente = await _abrir(tid, WEB)
    recorded.clear()

    await _recibir(tid, cliente, "1", WEB)

    assert recorded == ["1"] and herramienta.llamadas == []


async def test_una_nota_de_voz_o_un_adjunto_no_es_una_eleccion(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, recorded  # noqa: F811
) -> None:
    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, QR)
    send_id = await crear_menu_de_texto(tid, conv)
    recorded.clear()

    await _recibir(tid, cliente, "1", QR, media=json.dumps([{"type": "audio/ogg", "id": "x"}]))

    assert herramienta.llamadas == [] and await _activaciones(tid, send_id) == []


# ---------------------------------------------------------------- acciones sensibles
async def test_una_opcion_que_pide_confirmacion_no_se_ejecuta_por_texto(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    from core.kernel.opciones.textos import texto

    tid = cada_tenant_con_la_bandera_encendida
    await _bandera(tid, True)
    await _binding(tid, confirmar=True)  # el binding exige confirmación: la acción es sensible
    try:
        conv, cliente = await _abrir(tid, WEB)
        r = await _menu(tid, conv)
        recorded.clear()
        antes = len(await _salientes(tid, conv))

        await _recibir(tid, cliente, "1", WEB)

        assert recorded == [], "tampoco llega al agente: se responde un texto fijo"
        assert herramienta.llamadas == [], "la tool NO se llamó"
        assert (await _salientes(tid, conv))[antes:] == [texto("sensible_por_texto")]
        assert await _activaciones(tid, r.send_id) == [], "sin activación"
        [(entrada, salida)] = await _auditoria(tid, conv)
        assert entrada["via"] == "text" and salida["outcome"] == "sensitive_via_text" and salida["codigo"] == "sensible_por_texto"
    finally:
        await _binding(tid)


# ---------------------------------------------------------------- seguridad
async def test_un_payload_firmado_escrito_a_mano_por_el_cliente_no_ejecuta_nada(  # type: ignore[no-untyped-def]
    cada_tenant_con_la_bandera_encendida, herramienta, cola, recorded  # noqa: F811
) -> None:
    """El cliente solo manda texto: ni siquiera el payload VÁLIDO de la opción, copiado y pegado como mensaje, vale
    como toque (un toque exige el campo `interactive_id` que solo pone un proveedor de botones)."""
    from core.kernel.opciones import payload

    tid = cada_tenant_con_la_bandera_encendida
    await _binding(tid)
    await _bandera(tid, True)
    conv, cliente = await _abrir(tid, WEB)
    r = await _menu(tid, conv)
    valido = payload.construir(payload.secreto_del_servidor(), "opt", tid, conv, r.send_id, "o1")
    recorded.clear()

    await _recibir(tid, cliente, valido, WEB)
    await _recibir(tid, cliente, "opt:" + "a" * 32 + ":o1:" + "b" * 16, WEB)

    assert recorded == [valido, "opt:" + "a" * 32 + ":o1:" + "b" * 16]
    assert herramienta.llamadas == [] and await _activaciones(tid, r.send_id) == []


async def test_un_menu_de_otro_negocio_no_se_alcanza(negocios, cola, herramienta, recorded, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """Aunque una conversación del negocio B tuviera como último saliente un mensaje que apunta al envío del negocio A,
    el negocio B no lo ve (RLS + filtro explícito): «1» llega al agente y nada se ejecuta."""
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, b, _ = negocios
    _inmediato(monkeypatch)
    for n in (a, b):
        await _binding(n.tid)
    conv_a, _cliente_a = await _abrir(a.tid, WEB)
    envio_a = await _menu(a.tid, conv_a)
    conv_b, cliente_b = await _abrir(b.tid, WEB)
    async with tenant_scope(b.tid):
        async with get_session() as s:
            s.add(
                Message(
                    tenant_id=b.tid, conversation_id=conv_b, direction="outbound", sender_type="agent", content="1. x",
                    status="sent", client_message_id=f"opt_send:{envio_a.send_id}:0",
                )
            )
    recorded.clear()

    await _recibir(b.tid, cliente_b, "1", WEB)

    assert recorded == ["1"] and herramienta.llamadas == []
    assert await _activaciones(a.tid, envio_a.send_id) == [] and await _auditoria(b.tid, conv_b) == []
