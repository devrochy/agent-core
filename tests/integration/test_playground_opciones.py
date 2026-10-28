"""SPEC-40 · probar una plantilla con opciones SIN Meta, desde el playground (canal `test`).

`POST /api/v1/admin/playground/options` envía la plantilla a la conversación de prueba y `POST …/tap` simula el
toque de un botón. Se prueba por HTTP con la base, la RLS, la firma y el AgentSpec REALES; lo único que se
intercepta es la cola de Celery (para comprobar que NO se usa) y el LLM (para comprobar que NO se invoca).

- Camino completo: envío → botones firmados guardados como entregados → toque → la tool corre UNA vez → respuesta
  fija guardada → cero invocaciones al LLM, cero encolados; el doble toque no reejecuta.
- `optc`/`optn`: una opción con `confirmar` pide confirmación con botones, cancelar no ejecuta y confirmar sí.
- Bandera apagada: 404 `opciones_directas_no_habilitadas` en las dos rutas y no sale nada.
- Firma: un payload manipulado o de otro negocio se descarta sin mensajes y sin ejecutar nada; el que no es de
  opciones es 422 (no cae a un turno del LLM).
- Permisos: dueño o master con escritura en `connections`, como el editor.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from test_opciones_api import _headers, _publicada, definicion
from test_opciones_api import listo as _listo
from test_opciones_api import negocios as _negocios
from test_opciones_envio import _sql
from test_opciones_envio import cola as _cola
from test_opciones_toque import PARAMETROS, TOOL  # noqa: F401  (los usa la fixture `herramienta`)
from test_opciones_toque_entorno import _bandera, _LlmProhibido
from test_opciones_toque_entorno import herramienta as _herramienta

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Las fixtures de los tests hermanos, re-expuestas por asignación (importarlas por nombre las redefiniría como parámetro).
cola = _cola
herramienta = _herramienta
negocios = _negocios
listo = _listo

PG = "/api/v1/admin/playground"
DESCARTADOS = {"bad_signature", "unknown_send", "foreign_conversation"}


# ---------------------------------------------------------------- ayudas
def con_confirmacion() -> dict[str, Any]:
    d = definicion()
    d["opciones"][0]["accion"]["confirmar"] = True
    return d


async def _conv_de_prueba(tid: uuid.UUID) -> uuid.UUID:
    [(conv,)] = await _sql(
        tid,
        "select id from conversations where tenant_id = :t and channel = 'test' and status != 'closed' "
        "order by started_at desc limit 1",
        t=tid,
    )
    return conv


async def _salientes(tid: uuid.UUID) -> list[Any]:
    conv = await _conv_de_prueba(tid)
    return await _sql(
        tid,
        "select content, status, interactive, client_message_id from messages "
        "where conversation_id = :c and direction = 'outbound' order by created_at",
        c=conv,
    )


async def _enviar(client, h: dict[str, str], clave: str) -> dict[str, Any]:
    r = await client.post(f"{PG}/options", json={"clave": clave}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _tocar(client, h: dict[str, str], payload: str):
    return await client.post(f"{PG}/tap", json={"payload": payload}, headers=h)


def _botones(mensaje: dict[str, Any]) -> list[dict[str, Any]]:
    return mensaje["interactive"]["buttons"]


@pytest.fixture
def sin_llm(monkeypatch):  # type: ignore[no-untyped-def]
    _LlmProhibido.llamadas = 0
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: _LlmProhibido())

    async def _no(*_a: Any, **_k: Any) -> None:
        raise AssertionError("el toque del playground no puede correr un turno del agente")

    monkeypatch.setattr("core.orchestrator.loop.process_conversation", _no)
    return _LlmProhibido


# ---------------------------------------------------------------- camino completo
async def test_envio_boton_toque_respuesta_fija_con_la_tool_una_sola_vez_y_sin_llm(app_client, listo, herramienta, cola, sin_llm) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)

    enviado = await _enviar(app_client, h, clave)

    assert enviado["enviado"] and enviado["via"] == "interactivo" and enviado["ejecucion_directa"] and not enviado["degradado"]
    [mensaje] = enviado["mensajes"]
    assert mensaje["direction"] == "outbound" and mensaje["content"].startswith("Hola ")
    botones = _botones(mensaje)
    assert [b["title"] for b in botones] == ["Opción 1", "Opción 2"] and all(b["id"].startswith("opt:") for b in botones)
    [(_, estado, interactivo, _)] = await _salientes(a.tid)
    assert estado == "sent" and interactivo["buttons"] == botones, "guardado como entregado, con los payloads firmados"
    assert cola == [], "el playground no pasa por Celery"

    primero = await _tocar(app_client, h, botones[0]["id"])

    assert primero.status_code == 200, primero.text
    assert primero.json()["outcome"] == "ok"
    [respuesta] = primero.json()["mensajes"]
    assert respuesta["direction"] == "outbound" and respuesta["content"].startswith("Listo, ") and respuesta["interactive"] is None
    assert [args for args, _ in herramienta.llamadas] == [{"pedido": "A-1"}]
    assert sin_llm.llamadas == 0 and cola == []
    salientes = await _salientes(a.tid)
    assert [s[1] for s in salientes] == ["sent", "sent"], "el envío y la respuesta fija, ambos entregados"

    doble = await _tocar(app_client, h, botones[0]["id"])
    otra = await _tocar(app_client, h, botones[1]["id"])

    assert doble.json()["outcome"] == "duplicate" and otra.json()["outcome"] == "duplicate", "envío exclusivo: una sola opción"
    assert len(herramienta.llamadas) == 1, "la tool corrió una sola vez"
    [(n_envio,)] = await _sql(a.tid, "select count(*) from audit_log where action = 'option_send' and conversation_id = :c", c=await _conv_de_prueba(a.tid))
    [(n_toques,)] = await _sql(a.tid, "select count(*) from audit_log where action = 'option_tap' and conversation_id = :c", c=await _conv_de_prueba(a.tid))
    assert n_envio == 1 and n_toques >= 1


async def test_opcion_con_confirmar_pide_optc_y_optn_y_solo_optc_ejecuta(app_client, listo, herramienta, cola, sin_llm) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h, con_confirmacion())

    # Cancelar no ejecuta y libera la opción.
    botones = _botones((await _enviar(app_client, h, clave))["mensajes"][0])
    pedida = await _tocar(app_client, h, botones[0]["id"])
    assert pedida.json()["outcome"] == "confirm_requested" and herramienta.llamadas == []
    [pregunta] = pedida.json()["mensajes"]
    ids = [b["id"] for b in _botones(pregunta)]
    assert [i.split(":")[0] for i in ids] == ["optc", "optn"], "Confirmar y Cancelar, como botones del playground"
    assert (await _tocar(app_client, h, ids[1])).json()["outcome"] == "cancelled"
    assert herramienta.llamadas == []

    # Confirmar ejecuta una vez.
    again = await _tocar(app_client, h, botones[0]["id"])
    assert again.json()["outcome"] == "confirm_requested"
    optc = _botones(again.json()["mensajes"][0])[0]["id"]
    hecho = await _tocar(app_client, h, optc)
    assert hecho.json()["outcome"] == "ok" and hecho.json()["mensajes"][0]["content"].startswith("Listo, ")
    assert (await _tocar(app_client, h, optc)).json()["outcome"] != "ok"
    assert [args for args, _ in herramienta.llamadas] == [{"pedido": "A-1"}]
    assert sin_llm.llamadas == 0 and cola == []


# ---------------------------------------------------------------- bandera apagada
async def test_con_la_bandera_apagada_ni_envia_ni_toca(app_client, listo, herramienta, cola) -> None:
    a, _, c = listo
    hc = await _headers(app_client, c.tid)

    for ruta, cuerpo in (("options", {"clave": "x"}), ("tap", {"payload": "opt:" + "a" * 32 + ":o1:" + "b" * 16})):
        r = await app_client.post(f"{PG}/{ruta}", json=cuerpo, headers=hc)
        assert r.status_code == 404 and r.json()["error"]["code"] == "opciones_directas_no_habilitadas", (ruta, r.text)
    assert herramienta.llamadas == [] and cola == []

    # Apagarla DESPUÉS de enviar: el botón que ya está en pantalla deja de funcionar.
    ha = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, ha)
    botones = _botones((await _enviar(app_client, ha, clave))["mensajes"][0])
    await _bandera(a.tid, False)
    try:
        r = await _tocar(app_client, ha, botones[0]["id"])
        assert r.status_code == 404 and r.json()["error"]["code"] == "opciones_directas_no_habilitadas"
    finally:
        await _bandera(a.tid, True)
    assert herramienta.llamadas == []


# ---------------------------------------------------------------- firma y aislamiento
async def test_payload_manipulado_se_descarta_sin_ejecutar_y_sin_mensajes(app_client, listo, herramienta, cola, sin_llm) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)
    botones = _botones((await _enviar(app_client, h, clave))["mensajes"][0])
    original = botones[0]["id"]
    antes = len(await _salientes(a.tid))

    firma_rota = original[:-1] + ("0" if original[-1] != "0" else "1")
    prefijo, send, opcion, firma = original.split(":")
    otra_opcion = ":".join((prefijo, send, "o2", firma))  # la firma es de la opción 1
    inventado = ":".join((prefijo, uuid.uuid4().hex, opcion, firma))
    for manipulado in (firma_rota, otra_opcion, inventado):
        r = await _tocar(app_client, h, manipulado)
        assert r.status_code == 200 and r.json()["outcome"] in DESCARTADOS and r.json()["mensajes"] == [], manipulado

    assert herramienta.llamadas == [] and sin_llm.llamadas == 0
    assert len(await _salientes(a.tid)) == antes, "un payload que no valida se descarta sin responder"
    # y el botón auténtico sigue funcionando
    assert (await _tocar(app_client, h, original)).json()["outcome"] == "ok"


async def test_lo_que_no_es_de_opciones_no_llega_al_llm(app_client, listo, herramienta, sin_llm) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)

    for basura in ("hola", "proc:pagar", "opcion:x", ""):
        r = await _tocar(app_client, h, basura)
        assert r.status_code == 422 and r.json()["error"]["code"] == "payload_invalido", basura
    assert (await app_client.post(f"{PG}/tap", json={"payload": "opt:" + "a" * 300}, headers=h)).status_code == 422
    assert herramienta.llamadas == [] and sin_llm.llamadas == 0


async def test_aislamiento_el_boton_de_un_negocio_no_sirve_en_otro(app_client, listo, herramienta, cola, sin_llm) -> None:
    a, b, _ = listo
    ha, hb = await _headers(app_client, a.tid), await _headers(app_client, b.tid)
    clave = await _publicada(app_client, ha)
    botones = _botones((await _enviar(app_client, ha, clave))["mensajes"][0])

    # B (bandera encendida) toca el botón de A, y le pide a SU playground enviar la clave de A.
    r = await _tocar(app_client, hb, botones[0]["id"])
    assert r.status_code == 200 and r.json()["outcome"] in DESCARTADOS and r.json()["mensajes"] == []
    ajena = await app_client.post(f"{PG}/options", json={"clave": clave}, headers=hb)
    assert ajena.status_code == 404 and ajena.json()["error"]["code"] == "not_found"

    assert herramienta.llamadas == [], "la tool de A no corrió por el toque de B"
    assert await _sql(b.tid, "select 1 from messages where direction = 'outbound' and client_message_id like 'opt_send:%'") == []
    assert await _sql(b.tid, "select 1 from option_sends") == []
    # A sigue pudiendo tocar el suyo.
    assert (await _tocar(app_client, ha, botones[0]["id"])).json()["outcome"] == "ok"
    assert sin_llm.llamadas == 0 and cola == []


# ---------------------------------------------------------------- permisos y errores
async def test_permisos_dueno_o_master_con_escritura_y_errores_del_envio(app_client, listo, herramienta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)
    escritor = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "write"})
    lector = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "read"})
    dueno = await _headers(app_client, a.tid, rol="owner", secciones={})
    cuerpo_tap = {"payload": "opt:" + "a" * 32 + ":o1:" + "b" * 16}

    for sin_permiso in (escritor, lector):
        assert (await app_client.post(f"{PG}/options", json={"clave": clave}, headers=sin_permiso)).status_code == 403
        assert (await app_client.post(f"{PG}/tap", json=cuerpo_tap, headers=sin_permiso)).status_code == 403
    assert (await app_client.post(f"{PG}/options", json={"clave": clave})).status_code == 401
    assert (await app_client.post(f"{PG}/tap", json=cuerpo_tap)).status_code == 401
    assert herramienta.llamadas == []

    assert (await app_client.post(f"{PG}/options", json={"clave": clave}, headers=dueno)).status_code == 200
    inexistente = await app_client.post(f"{PG}/options", json={"clave": "no_existe"}, headers=h)
    assert inexistente.status_code == 404 and inexistente.json()["error"]["code"] == "not_found"
    sin_publicar = await app_client.post(f"{URL_PLANTILLAS}", json={"clave": "borrador_x", "nombre": "x", "definicion": definicion()}, headers=h)
    assert sin_publicar.status_code == 201
    r = await app_client.post(f"{PG}/options", json={"clave": "borrador_x"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_publicada"


URL_PLANTILLAS = "/api/v1/plantillas-opciones"


# ---------------------------------------------------------------- el sender no escribe en conversaciones ajenas
async def test_el_sender_de_prueba_no_escribe_en_la_conversacion_de_otro_negocio(app_client, listo) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.gateway import sender
    from core.shared.tenant import tenant_scope

    a, b, _ = listo
    ha = await _headers(app_client, a.tid)
    await app_client.post(f"{PG}/message", json={"text": "hola"}, headers=ha)  # crea la conversación de prueba de A (puede escalar: no importa)
    conv_a = await _conv_de_prueba(a.tid)
    antes = len(await _sql(a.tid, "select 1 from messages where conversation_id = :c", c=conv_a))
    interactivo = {"type": "button", "body": "Hola", "buttons": [{"id": "opt:x", "title": "Uno"}]}

    async with tenant_scope(b.tid):
        assert await sender.send_to_conversation(conv_a, "texto de B") is False
        assert await sender.send_interactive_to_conversation(conv_a, interactivo, fallback_text="x", sin_degradar=True) is False
    async with tenant_scope(a.tid):
        async with get_session() as s:
            fila = await s.get(Conversation, conv_a)
            s.expunge_all()
    async with tenant_scope(b.tid):  # la guarda propia del sender, aunque la fila llegara por otro camino
        assert await sender._guardar_saliente_de_prueba(fila, "texto de B", "agent") is False
    async with tenant_scope(None):
        assert await sender._guardar_saliente_de_prueba(fila, "sin negocio", "agent") is False

    despues = len(await _sql(a.tid, "select 1 from messages where conversation_id = :c", c=conv_a))
    assert despues == antes, "ni una fila nueva en la conversación de A"
