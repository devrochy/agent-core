"""SPEC-40 · el envío de una plantilla con opciones, SIN base ni red (HU-OPC-02, ADR-0172 §2-§3).

Fija: la cascada de vías (ventana abierta/cerrada/desconocida, canal sin botones, plantilla no utilizable), la forma
de los componentes de plantilla con payload —el ÚNICO punto que depende de una suposición sin verificar con Meta—,
que cada payload lo valida el verificador del PR 3 y solo en SU conversación, la compatibilidad con la contraparte de
Meta, el contexto que arma el servidor, y la tool `enviar_plantilla_opciones` (de solo envío, con bandera y no
elegible como acción de una opción).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.kernel.opciones import contexto, envio, lint, mapeo, payload
from core.kernel.opciones.definicion import Definicion

AHORA = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONV = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTRA_CONV = uuid.UUID("33333333-3333-4333-8333-333333333333")
SEND = uuid.UUID("44444444-4444-4444-8444-444444444444")
SECRETO = "secreto-de-prueba"


def definicion(n: int = 3, *, body: str = "Hola {cliente.nombre}, ¿qué hacemos?", variables: list[str] | None = None, **extra: Any) -> Definicion:
    return Definicion.model_validate(
        {
            "body": body,
            "footer": extra.pop("footer", None),
            "variables": ["cliente.nombre"] if variables is None else variables,
            "ttl_horas": extra.pop("ttl_horas", 24),
            "opciones": [
                {
                    "id": f"o{i}",
                    "titulo": f"Opción {i}",
                    "accion": {"tool": "consultar_estado_pedido", "args_fijos": {"pedido": f"A-{i}"}},
                    "respuesta_ok": "Listo.",
                    "respuesta_error": "No pude.",
                }
                for i in range(1, n + 1)
            ],
            **extra,
        }
    )


def payloads(d: Definicion, conv: uuid.UUID = CONV) -> dict[str, str]:
    return envio.payloads_de(d, lambda oid: payload.construir(SECRETO, "opt", TENANT, conv, SEND, oid))


# ---------------------------------------------------------------- cascada
def decidir(**cambios: Any) -> envio.Decision:
    base: dict[str, Any] = dict(
        canal="whatsapp", proveedor="meta", ventana_abierta=True, interactivo_valido=True, plantilla_utilizable=False
    )
    return envio.decidir_via(**{**base, **cambios})


def test_ventana_abierta_en_whatsapp_cloud_sale_interactivo() -> None:
    assert decidir() == envio.Decision(envio.INTERACTIVO)


def test_ventana_abierta_con_payload_fuera_de_topes_degrada_a_texto_sin_ejecucion_directa() -> None:
    assert decidir(interactivo_valido=False) == envio.Decision(envio.TEXTO, envio.FUERA_DE_TOPES)


def test_ventana_cerrada_con_plantilla_utilizable_sale_plantilla() -> None:
    assert decidir(ventana_abierta=False, plantilla_utilizable=True) == envio.Decision(envio.PLANTILLA)


@pytest.mark.parametrize(
    "motivo", [envio.SIN_VERSION_EN_META, envio.PLANTILLA_NO_APROBADA, envio.PLANTILLA_OTRA_CUENTA, envio.PLANTILLA_DISTINTA]
)
def test_ventana_cerrada_sin_plantilla_utilizable_no_envia_nada(motivo: str) -> None:
    """Pendiente, rechazada, pausada, de otra cuenta, distinta o sin publicar: un texto libre fuera de la ventana lo
    rechaza Meta (131047), así que `via` es None y quien llama deja el mensaje `failed` con `sin_plantilla`."""
    assert decidir(ventana_abierta=False, motivo_plantilla=motivo) == envio.Decision(None, motivo)


@pytest.mark.parametrize(
    ("canal", "proveedor"),
    [("web", ""), ("instagram", ""), ("messenger", ""), ("email", ""), ("whatsapp", "waweb"), ("whatsapp", "")],
)
def test_canal_sin_botones_sale_texto_aunque_haya_ventana_y_plantilla(canal: str, proveedor: str) -> None:
    assert decidir(canal=canal, proveedor=proveedor, plantilla_utilizable=True) == envio.Decision(envio.TEXTO, envio.CANAL_SIN_BOTONES)


def test_ventana_desconocida_cuenta_como_cerrada() -> None:
    assert envio.ventana_abierta(None, AHORA) is False
    assert envio.ventana_abierta(AHORA - timedelta(seconds=1), AHORA) is False
    assert envio.ventana_abierta(AHORA, AHORA) is True
    assert envio.ventana_abierta(AHORA + timedelta(hours=3), AHORA) is True


def test_expira_en_usa_el_ttl_de_la_definicion_acotado_a_1_hora_y_30_dias() -> None:
    assert envio.expira_en(AHORA, 24) == AHORA + timedelta(hours=24)
    assert envio.expira_en(AHORA, 168) == AHORA + timedelta(days=7)
    assert envio.expira_en(AHORA, 0) == AHORA + timedelta(hours=1)
    assert envio.expira_en(AHORA, 10_000) == AHORA + timedelta(days=30)


# ---------------------------------------------------------------- componentes con payload (UN solo punto)
def test_componentes_con_payload_fija_la_forma_supuesta_de_meta() -> None:
    """GOLDEN de la forma SIN VERIFICAR con Meta (`kernel/opciones/envio.py`, ADR-0172 §3): un componente `button`
    por botón, `sub_type: quick_reply`, `index` como texto y un parámetro `payload`. Si la prueba real en el
    sandbox muestra otra forma, se cambia `componentes_con_payload` y SOLO este golden."""
    assert envio.componentes_con_payload(["opt:a", "opt:b"]) == [
        {"type": "button", "sub_type": "quick_reply", "index": "0", "parameters": [{"type": "payload", "payload": "opt:a"}]},
        {"type": "button", "sub_type": "quick_reply", "index": "1", "parameters": [{"type": "payload", "payload": "opt:b"}]},
    ]


def test_componentes_de_plantilla_lleva_el_cuerpo_primero_y_un_boton_por_opcion() -> None:
    d = definicion(3)
    cuerpo = mapeo.parametros_de_cuerpo_meta(d, {"cliente.nombre": "Ana"})
    ps = payloads(d)
    componentes = envio.componentes_de_plantilla(cuerpo, [ps[o.id] for o in d.opciones])
    assert [c["type"] for c in componentes] == ["body", "button", "button", "button"]
    assert componentes[0]["parameters"] == [{"type": "text", "text": "Ana", "parameter_name": "cliente_nombre"}]
    assert [c["index"] for c in componentes[1:]] == ["0", "1", "2"]
    assert [c["parameters"][0]["payload"] for c in componentes[1:]] == [ps["o1"], ps["o2"], ps["o3"]]


def test_la_plantilla_sin_variables_no_lleva_componente_de_cuerpo() -> None:
    d = definicion(2, body="¿Qué hacemos?", variables=[])
    componentes = envio.componentes_de_plantilla(mapeo.parametros_de_cuerpo_meta(d, {}), ["opt:a", "opt:b"])
    assert [c["type"] for c in componentes] == ["button", "button"]


# ---------------------------------------------------------------- payloads firmados
def test_cada_payload_cabe_valida_con_el_verificador_del_pr3_y_solo_en_su_conversacion() -> None:
    d = definicion(10)
    ps = payloads(d)
    assert list(ps) == [o.id for o in d.opciones]
    for oid, p in ps.items():
        assert len(p) <= payload.MAX_PAYLOAD <= 256
        leido = payload.leer(p)
        assert leido is not None and leido.tipo == "opt" and leido.send_id == SEND and leido.option_id == oid
        assert payload.firma_valida(leido, SECRETO, TENANT, CONV)
        assert not payload.firma_valida(leido, SECRETO, TENANT, OTRA_CONV), "un botón reenviado a otra conversación no valida"
        assert not payload.firma_valida(leido, "otro-secreto", TENANT, CONV)
        assert not payload.firma_valida(leido, SECRETO, uuid.uuid4(), CONV), "ni en otro negocio"


def test_dos_envios_de_la_misma_opcion_a_conversaciones_distintas_dan_payloads_distintos() -> None:
    d = definicion(2)
    a, b = payloads(d, CONV), payloads(d, OTRA_CONV)
    assert a["o1"] != b["o1"]
    leido_a = payload.leer(a["o1"])
    assert leido_a is not None and not payload.firma_valida(leido_a, SECRETO, TENANT, OTRA_CONV)


# ---------------------------------------------------------------- compatibilidad con la plantilla de Meta
def aprobada(d: Definicion) -> list[dict[str, Any]]:
    return mapeo.componentes_meta(d)


def test_la_plantilla_generada_de_la_definicion_es_compatible() -> None:
    d = definicion(3)
    assert envio.compatibilidad_plantilla(aprobada(d), d) is None


def test_una_plantilla_con_otros_botones_no_es_compatible_porque_el_payload_va_por_posicion() -> None:
    d = definicion(3)
    otros = aprobada(d)
    otros[-1]["buttons"][0], otros[-1]["buttons"][1] = otros[-1]["buttons"][1], otros[-1]["buttons"][0]
    assert envio.compatibilidad_plantilla(otros, d) == envio.PLANTILLA_DISTINTA
    menos = aprobada(d)
    menos[-1]["buttons"].pop()
    assert envio.compatibilidad_plantilla(menos, d) == envio.PLANTILLA_DISTINTA
    cambiado = aprobada(d)
    cambiado[-1]["buttons"][2]["text"] = "Cancelar todo"
    assert envio.compatibilidad_plantilla(cambiado, d) == envio.PLANTILLA_DISTINTA


def test_una_plantilla_con_otras_variables_o_botones_que_no_son_quick_reply_no_es_compatible() -> None:
    d = definicion(2)
    componentes = aprobada(d)
    componentes[0]["text"] = "Hola {{1}}"
    assert envio.compatibilidad_plantilla(componentes, d) == envio.PLANTILLA_DISTINTA
    con_url = aprobada(d)
    con_url[-1]["buttons"].append({"type": "URL", "text": "Web", "url": "https://x.co"})
    assert envio.compatibilidad_plantilla(con_url, d) == envio.PLANTILLA_DISTINTA
    var_en_pie = [*aprobada(d), {"type": "FOOTER", "text": "Hola {{x}}"}]
    assert envio.compatibilidad_plantilla(var_en_pie, d) == envio.PLANTILLA_DISTINTA


# ---------------------------------------------------------------- contexto armado por el servidor
BASE = {"cliente.nombre": "Ana", "negocio.nombre": "Tienda Sol"}


def con_acciones(args_contexto: dict[str, str], ok: str = "Listo.") -> Definicion:
    return Definicion.model_validate(
        {
            "body": "Hola {cliente.nombre}",
            "variables": ["cliente.nombre"],
            "opciones": [
                {
                    "id": "ver",
                    "titulo": "Ver",
                    "accion": {"tool": "t", "args_contexto": args_contexto},
                    "respuesta_ok": ok,
                    "respuesta_error": "No pude.",
                }
            ],
        }
    )


def test_el_contexto_guarda_solo_las_rutas_que_la_definicion_cita() -> None:
    d = con_acciones({"cliente": "cliente.nombre"}, ok="{ctx.negocio.nombre}: listo")
    assert contexto.armar(d, BASE) == {"cliente.nombre": "Ana", "negocio.nombre": "Tienda Sol"}
    assert contexto.armar(definicion(2), BASE) == {"cliente.nombre": "Ana"}, "el nombre del negocio no se cita: no se guarda"


def test_falta_una_ruta_necesaria_no_se_envia_y_el_error_lleva_solo_nombres() -> None:
    with pytest.raises(contexto.ContextoIncompleto) as exc:
        contexto.armar(definicion(2), {"cliente.nombre": "  "})
    assert exc.value.rutas == ["cliente.nombre"]
    with pytest.raises(contexto.ContextoIncompleto) as exc:
        contexto.armar(con_acciones({"fecha": "cita.fecha"}), BASE)
    assert exc.value.rutas == ["cita.fecha"]
    assert "Ana" not in str(exc.value)


def test_una_cita_de_respuesta_sin_dato_es_opcional() -> None:
    d = con_acciones({}, ok="{ctx.cita.fecha}")
    assert contexto.armar(d, BASE) == {"cliente.nombre": "Ana"}


@pytest.fixture
def rutas_de_datos(monkeypatch: pytest.MonkeyPatch) -> None:
    """La lista permitida de rutas de `datos` está vacía (ADR-0172, PR 6): estos tests agregan las suyas."""
    monkeypatch.setitem(contexto.RUTAS_DE_DATOS, "cita.fecha", "Fecha de la cita")
    monkeypatch.setitem(contexto.RUTAS_DE_DATOS, "cita.otra", "Otro dato de la cita")


def test_los_datos_del_servidor_aportan_rutas_extra_pero_no_tapan_las_de_la_base(rutas_de_datos) -> None:
    d = con_acciones({"fecha": "cita.fecha"})
    assert contexto.armar(d, BASE, {"cita.fecha": "2026-11-02"}) == {"cliente.nombre": "Ana", "cita.fecha": "2026-11-02"}
    with pytest.raises(contexto.DatosInvalidos):
        contexto.armar(d, BASE, {"cliente.nombre": "Mallory"})
    with pytest.raises(contexto.DatosInvalidos):
        contexto.armar(d, BASE, {"Cita Fecha": "x"})


def test_los_valores_se_limpian_y_lo_que_no_es_un_dato_simple_no_cuenta(rutas_de_datos) -> None:
    d = con_acciones({"fecha": "cita.fecha", "otro": "cita.otra"})
    with pytest.raises(contexto.ContextoIncompleto) as exc:
        contexto.armar(d, BASE, {"cita.fecha": ["no", "simple"], "cita.otra": {"x": 1}})
    assert exc.value.rutas == ["cita.fecha", "cita.otra"]
    limpio = contexto.armar(con_acciones({"fecha": "cita.fecha"}), {"cliente.nombre": "Ana\n  Pérez"}, {"cita.fecha": 20261102})
    assert limpio == {"cliente.nombre": "Ana Pérez", "cita.fecha": "20261102"}


def test_el_nombre_del_cliente_se_acota_a_60_pero_los_datos_del_servidor_no(rutas_de_datos) -> None:
    d = con_acciones({"fecha": "cita.fecha"})
    largo = contexto.armar(d, {"cliente.nombre": "N" * 500}, {"cita.fecha": "F" * 200})
    assert largo["cliente.nombre"] == "N" * 60 and largo["cita.fecha"] == "F" * 200


def test_el_cuerpo_se_renderiza_con_el_contexto_saneado() -> None:
    d = definicion(2)
    assert mapeo.renderizar(d.body, contexto.armar(d, {"cliente.nombre": "Ana\nPérez"})) == "Hola Ana Pérez, ¿qué hacemos?"


# ---------------------------------------------------------------- texto de respaldo y resultado para el agente
def test_texto_de_respaldo_numera_las_opciones() -> None:
    assert mapeo.texto_de_respaldo(definicion(3), "Hola Ana") == "Hola Ana\n\n1. Opción 1\n2. Opción 2\n3. Opción 3"


def test_el_resultado_para_el_agente_no_lleva_ids_ni_contexto() -> None:
    ok = envio.ResultadoEnvio(True, envio.ENVIADO, via=envio.INTERACTIVO, send_id=SEND)
    assert envio.para_el_agente(ok) == {"status": "sent", "via": "interactivo", "ejecucion_directa": True}
    texto = envio.ResultadoEnvio(True, envio.ENVIADO, via=envio.TEXTO, send_id=SEND, motivo=envio.CANAL_SIN_BOTONES, degradado=True)
    assert envio.para_el_agente(texto) == {"status": "sent", "via": "texto", "ejecucion_directa": False}
    assert envio.para_el_agente(envio.ResultadoEnvio(False, envio.DUPLICADO, send_id=SEND)) == {"status": "already_sent"}
    for outcome in (envio.BANDERA_APAGADA, envio.NO_ENCONTRADA, envio.NO_PUBLICADA, envio.SIN_PLANTILLA, envio.CONTEXTO_INCOMPLETO):
        r = envio.para_el_agente(envio.ResultadoEnvio(False, outcome, send_id=SEND, motivo="cliente.nombre"))
        assert set(r) == {"error", "mensaje"} and r["error"] == outcome
        assert str(SEND) not in str(r) and "cliente.nombre" not in str(r)


# ---------------------------------------------------------------- pie del interactivo
def test_el_pie_viaja_en_el_interactivo_y_se_valida() -> None:
    from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
    from core.gateway.sender import interactivo_valido

    d = definicion(4, footer="Responde antes del viernes")
    interactivo = mapeo.a_interactivo(d, "Hola", payloads(d))
    assert interactivo["type"] == "list" and interactivo["footer"] == "Responde antes del viernes"
    assert interactivo_valido(interactivo)
    assert WhatsAppMetaOutbound._payload_lista("573001112233", interactivo)["interactive"]["footer"] == {"text": "Responde antes del viernes"}
    assert not interactivo_valido({**interactivo, "footer": "x" * 61})
    assert "footer" not in WhatsAppMetaOutbound._payload_lista("573001112233", {**interactivo, "footer": "  "})["interactive"]


# ---------------------------------------------------------------- la tool del agente
def tool_core():
    from core.tools.core_skill import CoreSkill

    return next(t for t in CoreSkill().get_tools() if t.name == "enviar_plantilla_opciones")


def test_la_tool_es_de_solo_envio_con_un_argumento_minimo() -> None:
    tool = tool_core()
    assert set(tool.parameters["properties"]) == {"clave"} and tool.parameters["required"] == ["clave"]
    assert tool.parameters["additionalProperties"] is False
    assert tool.parameters["properties"]["clave"]["pattern"] == "^[a-z0-9_]{1,40}$"
    assert tool.always_available is True and tool.plugin_id in ("", "core")
    assert len(tool.description.splitlines()) <= 3 and "NO crea ni edita" in tool.description
    assert tool.solo_lectura is False and not tool.scopes


def test_la_tool_no_es_elegible_como_accion_de_una_opcion() -> None:
    """Una opción que enviara otra plantilla de opciones abre un bucle de botones que envían botones."""
    tool = tool_core()
    assert tool.apta_para_opcion is False and tool.sensible is False
    ficha = lint.fichas_de_tools([tool], [], {"core"})[tool.name]
    assert "tool_no_apta" in [codigo for codigo, _ in lint.motivos_de_rechazo(tool.name, ficha)]


@pytest.mark.asyncio
async def test_la_tool_solo_se_ofrece_con_la_bandera_encendida() -> None:
    from core.kernel.opciones import bandera

    tool = tool_core()
    assert tool.connection_predicate is bandera.oferta_habilitada
    assert await bandera.oferta_habilitada(set()) is False, "sin negocio en el contexto no se ofrece"


@pytest.mark.asyncio
async def test_el_handler_solo_envia_a_la_conversacion_del_turno_con_su_clave_de_idempotencia(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.mcp.types import ToolContext
    from core.tools.core_skill import CoreSkill

    capturado: dict[str, Any] = {}

    async def falso(conversation_id: uuid.UUID, clave: str, **kwargs: Any) -> envio.ResultadoEnvio:
        capturado.update(conversation_id=conversation_id, clave=clave, **kwargs)
        return envio.ResultadoEnvio(True, envio.ENVIADO, via=envio.PLANTILLA, send_id=SEND)

    monkeypatch.setattr("core.gateway.sender.send_options_to_conversation", falso)
    ctx = ToolContext(conversation_id=CONV, customer_id=uuid.uuid4(), idempotency_key=f"{CONV}:call_1", channel="whatsapp")
    salida = await CoreSkill()._enviar_plantilla_opciones({"clave": " aviso_cobro ", "conversation_id": str(OTRA_CONV), "datos": {"x": 1}}, ctx)

    assert salida == {"status": "sent", "via": "plantilla", "ejecucion_directa": True}
    assert capturado == {"conversation_id": CONV, "clave": "aviso_cobro", "clave_idempotencia": f"{CONV}:call_1", "actor": "agent"}, (
        "ni la conversación ni los datos del contexto salen de lo que mande el modelo"
    )


def test_el_envio_cuenta_en_la_metrica_con_canal_y_via() -> None:
    from core.kernel.opciones import metricas

    metricas.envio("whatsapp", "interactivo")  # sin MeterProvider es un no-op: solo comprueba que existe y no lanza
