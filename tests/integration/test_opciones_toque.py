"""SPEC-40 · el toque de una opción directa contra Postgres real (HU-OPC-03, ADR-0172 §4-§5).

Dos capas:
- **Resolver con el entorno simulado**: la base, la firma y las activaciones son las reales; solo la tool (el
  borde `Entorno`) es un doble que cuenta ejecuciones. Cubre doble toque, reintento, confirmación, vencimiento,
  versión retirada, deriva, aislamiento entre negocios y `running` colgado.
- **Entorno real** (`core.orchestrator.opciones_toque`): la tool se registra en el host y corre por
  `_execute_tool_safely` con el spec y los bindings reales del negocio (legacy y v3), y el camino completo del
  consumer: bandera apagada = comportamiento de siempre; encendida = toque → respuesta fija con el LLM en cero.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TOOL = "opc_consultar_estado"
HASH = "b" * 64
PARAMETROS = {
    "type": "object",
    "properties": {"pedido": {"type": "string"}, "correo": {"type": "string"}},
    "required": ["pedido"],
}


# ---------------------------------------------------------------- datos
def definicion(*, exclusivo: bool = True, confirmar: bool = False, ok: str | None = None, args_contexto: dict | None = None) -> dict:
    return {
        "body": "Hola {cliente.nombre}, ¿qué hacemos?",
        "footer": None,
        "variables": ["cliente.nombre"],
        "exclusivo": exclusivo,
        "ttl_horas": 24,
        "opciones": [
            {
                "id": "ver",
                "titulo": "Ver estado",
                "accion": {
                    "tool": TOOL,
                    "args_fijos": {"pedido": "A-1"},
                    "args_contexto": args_contexto or {},
                    "confirmar": confirmar,
                },
                "respuesta_ok": ok if ok is not None else "{ctx.cliente.nombre}, tu pedido está {result.estado}.",
                "respuesta_error": "No pude consultarlo, {ctx.cliente.nombre}.",
                "result_keys": ["estado"],
            },
            {
                "id": "otra",
                "titulo": "Otra consulta",
                "accion": {"tool": TOOL, "args_fijos": {"pedido": "B-2"}, "args_contexto": {}, "confirmar": confirmar},
                "respuesta_ok": "Listo, B-2.",
                "respuesta_error": "No pude consultar B-2.",
                "result_keys": [],
            },
        ],
    }


def ficha(**cambios: Any):
    from core.kernel.opciones.lint import FichaTool

    base = dict(
        nombre=TOOL, parameters=PARAMETROS, apta_para_opcion=True, sensible=False, pack_activo=True,
        binding_habilitado=True, binding_pide_confirmacion=False, gobernada_por_procedimiento=False,
    )
    return FichaTool(**{**base, **cambios})


class Salida:
    def __init__(self) -> None:
        self.textos: list[str] = []
        self.confirmaciones: list[dict[str, Any]] = []
        self.puede_confirmar = True

    async def texto(self, cuerpo: str) -> bool:
        self.textos.append(cuerpo)
        return True

    async def confirmacion(self, interactivo: dict[str, Any]) -> bool:
        self.confirmaciones.append(interactivo)
        return self.puede_confirmar


class EntornoFalso:
    """El borde simulado: cuenta las ejecuciones de la tool."""

    def __init__(self, ficha_de_la_tool=None, *, denegacion: str | None = None, resultado=None, excepcion=None, demora=0.0) -> None:
        self._ficha = ficha_de_la_tool if ficha_de_la_tool is not None else ficha()
        self._denegacion = denegacion
        self.resultado = resultado if resultado is not None else {"estado": "en camino"}
        self.excepcion = excepcion
        self.demora = demora
        self.llamadas: list[tuple[str, dict[str, Any], uuid.UUID]] = []

    async def ficha(self, tool: str):
        return self._ficha

    async def denegacion(self, tool: str) -> str | None:
        return self._denegacion

    async def ejecutar(self, tool: str, args: dict[str, Any], activation_id: uuid.UUID) -> dict[str, Any]:
        self.llamadas.append((tool, dict(args), activation_id))
        if self.demora:
            await asyncio.sleep(self.demora)
        if self.excepcion is not None:
            raise self.excepcion
        return dict(self.resultado)


def bordes(salida: Salida, entorno: Any, *, ahora: Callable[[], datetime] | None = None):
    from core.kernel.opciones.resolver import Bordes

    @asynccontextmanager
    async def abrir() -> AsyncIterator[Any]:
        yield entorno

    extra = {"ahora": ahora} if ahora is not None else {}
    return Bordes(abrir_entorno=abrir, enviar_texto=salida.texto, enviar_confirmacion=salida.confirmacion, **extra)


class Negocio:
    def __init__(self, tid: uuid.UUID, slug: str) -> None:
        self.tid, self.slug = tid, slug


async def _scoped(tid: uuid.UUID, fn: Callable[[Any], Any]) -> Any:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await fn(s)


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async def _f(s: Any) -> list[Any]:
        r = await s.execute(text(sql), params)
        return list(r.all()) if getattr(r, "returns_rows", False) else []

    return await _scoped(tid, _f)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[Negocio, Negocio]]:
    from core.alta_negocio import onboard_tenant
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    salida: list[Negocio] = []
    for pref in ("opta", "optb"):
        slug = f"{pref}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Opciones {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        async with tenant_scope(tid):
            await bandera.fijar(tid, True, actor="test")
        salida.append(Negocio(tid, slug))
    yield salida[0], salida[1]
    for n in salida:
        await deactivate_tenant(str(n.tid))
        await purge_tenant(str(n.tid), f"ELIMINAR {n.slug}")


async def conversacion(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer

    async def _f(s: Any) -> tuple[uuid.UUID, uuid.UUID]:
        c = Customer(tenant_id=tid, channel="whatsapp", external_id=f"opt:{uuid.uuid4().hex[:8]}")
        s.add(c)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=c.id, channel="whatsapp")
        s.add(conv)
        await s.flush()
        return conv.id, c.id

    return await _scoped(tid, _f)


class Envio:
    def __init__(self, tid: uuid.UUID, conv: uuid.UUID, send_id: uuid.UUID, version_id: uuid.UUID, template_id: uuid.UUID) -> None:
        self.tid, self.conv, self.send_id, self.version_id, self.template_id = tid, conv, send_id, version_id, template_id

    def payload(self, tipo: str = "opt", opcion: str = "ver", args_hash: str = "") -> str:
        from core.kernel.opciones import payload

        return payload.construir(payload.secreto_del_servidor(), tipo, self.tid, self.conv, self.send_id, opcion, args_hash)  # type: ignore[arg-type]


async def crear_envio(
    tid: uuid.UUID, conv: uuid.UUID, definicion_: dict | None = None, *, contexto: dict | None = None,
    vence_en: timedelta = timedelta(hours=24), retirada: bool = False,
) -> Envio:
    clave = f"t{uuid.uuid4().hex[:10]}"
    template_id, version_id, send_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    import json

    async def _f(s: Any) -> None:
        await s.execute(
            text("insert into option_templates (id, tenant_id, key, name, status, current_version_n) values (:i, :t, :k, 'x', 'published', 1)"),
            {"i": template_id, "t": tid, "k": clave},
        )
        await s.execute(
            text(
                "insert into option_template_versions (id, tenant_id, option_template_id, version_n, definition, definition_hash, retired_at) "
                "values (:i, :t, :tp, 1, cast(:d as jsonb), :h, :r)"
            ),
            {"i": version_id, "t": tid, "tp": template_id, "d": json.dumps(definicion_ or definicion()), "h": HASH,
             "r": datetime.now(UTC) if retirada else None},
        )
        await s.execute(
            text(
                "insert into option_sends (id, tenant_id, option_template_id, version_id, conversation_id, via, context, expires_at) "
                "values (:i, :t, :tp, :v, :c, 'interactive', cast(:ctx as jsonb), :e)"
            ),
            {"i": send_id, "t": tid, "tp": template_id, "v": version_id, "c": conv,
             "ctx": json.dumps(contexto if contexto is not None else {"cliente.nombre": "Ana"}), "e": datetime.now(UTC) + vence_en},
        )

    await _scoped(tid, _f)
    return Envio(tid, conv, send_id, version_id, template_id)


async def tocar(envio: Envio, payload: str, entorno: Any, salida: Salida | None = None, *, wamid: str | None = None,
                ahora: Callable[[], datetime] | None = None, tenant: uuid.UUID | None = None,
                conversacion_: uuid.UUID | None = None) -> tuple[str, Salida]:
    from core.kernel.opciones.resolver import Toque, atender_toque
    from core.shared.tenant import tenant_scope

    salida = salida or Salida()
    tid = tenant or envio.tid
    async with tenant_scope(tid):
        outcome = await atender_toque(
            Toque(tenant_id=tid, conversation_id=conversacion_ or envio.conv, payload=payload, wamid=wamid or uniq("wamid")),
            bordes(salida, entorno, ahora=ahora),
        )
    return outcome, salida


async def estado(envio: Envio) -> list[tuple[str, str, str | None]]:
    filas = await _sql(envio.tid, "select option_id, state, error_code from option_activations where send_id = :s order by created_at", s=envio.send_id)
    return [(f[0], f[1], f[2]) for f in filas]


async def auditoria(envio: Envio) -> list[Any]:
    return await _sql(envio.tid, "select actor, action, input, output from audit_log where action = 'option_tap' and input->>'send_id' = :s", s=str(envio.send_id))


# ---------------------------------------------------------------- toque feliz y una sola ejecución
async def test_toque_ejecuta_la_tool_una_vez_y_responde_texto_fijo(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso()

    outcome, salida = await tocar(envio, envio.payload(), entorno)

    assert outcome == "ok"
    assert [(t, args) for t, args, _ in entorno.llamadas] == [(TOOL, {"pedido": "A-1"})]
    assert salida.textos == ["Ana, tu pedido está en camino."]
    assert await estado(envio) == [("ver", "done", None)]
    [fila] = await auditoria(envio)
    assert fila[0].startswith("option:t") and fila[1] == "option_tap"
    assert fila[3]["outcome"] == "ok" and fila[3]["claves"] == ["estado"]
    assert "A-1" not in str(fila[2]) and "Ana" not in str(fila[3]), "la auditoría lleva ids y claves, no valores"
    assert fila[2]["activation_id"] == str(entorno.llamadas[0][2]), "la tool recibe el id de la activación"


async def test_doble_toque_simultaneo_ejecuta_una_sola_vez(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(demora=0.2)

    resultados = await asyncio.gather(
        tocar(envio, envio.payload(), entorno), tocar(envio, envio.payload(), entorno), tocar(envio, envio.payload("opt", "otra"), entorno)
    )

    assert len(entorno.llamadas) == 1, "gana un solo worker, también si tocan otra opción de un envío exclusivo"
    assert sorted(o for o, _ in resultados).count("ok") == 1
    for outcome, salida in resultados:
        assert len(salida.textos) == 1, "nunca silencio"
        if outcome != "ok":
            assert outcome in {"in_progress", "duplicate"}


async def test_segundo_toque_repite_la_respuesta_fija_si_no_cita_resultado(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv, definicion(ok="Listo, {ctx.cliente.nombre}."))
    entorno = EntornoFalso()

    primero, s1 = await tocar(envio, envio.payload(), entorno)
    segundo, s2 = await tocar(envio, envio.payload(), entorno)

    assert (primero, segundo) == ("ok", "duplicate")
    assert s1.textos == s2.textos == ["Listo, Ana."]
    assert len(entorno.llamadas) == 1


async def test_segundo_toque_con_resultado_responde_ya_atendido(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso()

    await tocar(envio, envio.payload(), entorno)
    segundo, salida = await tocar(envio, envio.payload(), entorno)

    assert segundo == "duplicate" and salida.textos == [texto("ya_atendido")], "la tabla no guarda resultados"
    assert len(entorno.llamadas) == 1


async def test_reintento_del_webhook_con_el_mismo_wamid_no_ejecuta_de_nuevo(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv, definicion(exclusivo=False))
    entorno = EntornoFalso()

    await tocar(envio, envio.payload(), entorno, wamid="wamid.REINTENTO")
    await tocar(envio, envio.payload(), entorno, wamid="wamid.REINTENTO")

    assert len(entorno.llamadas) == 1


async def test_envio_no_exclusivo_deja_ejecutar_cada_opcion_una_vez(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv, definicion(exclusivo=False))
    entorno = EntornoFalso()

    await tocar(envio, envio.payload("opt", "ver"), entorno)
    await tocar(envio, envio.payload("opt", "otra"), entorno)
    await tocar(envio, envio.payload("opt", "otra"), entorno)

    assert [args["pedido"] for _, args, _ in entorno.llamadas] == ["A-1", "B-2"]


async def test_envio_exclusivo_agota_el_envio_con_la_primera_opcion(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso()

    await tocar(envio, envio.payload("opt", "ver"), entorno)
    otra, salida = await tocar(envio, envio.payload("opt", "otra"), entorno)

    assert otra == "duplicate" and len(entorno.llamadas) == 1 and len(salida.textos) == 1


# ---------------------------------------------------------------- fallos de la tool
async def test_tool_que_devuelve_error_marca_failed_y_responde_el_error(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(resultado={"error": "pedido_no_existe", "mensaje": "Ana Pérez, cuenta 123"})

    outcome, salida = await tocar(envio, envio.payload(), entorno)
    otra_vez, salida2 = await tocar(envio, envio.payload(), entorno)

    assert (outcome, otra_vez) == ("tool_error", "duplicate")
    assert salida.textos == salida2.textos == ["No pude consultarlo, Ana."]
    assert await estado(envio) == [("ver", "failed", "pedido_no_existe")], "solo un código, nunca el texto del error"
    assert len(entorno.llamadas) == 1, "una acción que falló tampoco se reejecuta sola"


async def test_excepcion_de_la_tool_responde_el_error_sin_romper(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)

    outcome, salida = await tocar(envio, envio.payload(), EntornoFalso(excepcion=RuntimeError("secreto 4111")))

    assert outcome == "tool_error" and salida.textos == ["No pude consultarlo, Ana."]
    [(_, estado_, codigo)] = await estado(envio)
    assert estado_ == "failed" and codigo == "excepcion_RuntimeError" and "4111" not in codigo


async def test_resultado_sin_la_clave_citada_usa_el_texto_generico(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)

    outcome, salida = await tocar(envio, envio.payload(), EntornoFalso(resultado={"otro": 1}))

    assert outcome == "ok" and salida.textos == [texto("ok_generico")]


async def test_valores_del_resultado_se_sanean_y_solo_cadenas_o_numeros(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)

    _, salida = await tocar(envio, envio.payload(), EntornoFalso(resultado={"estado": "en\ncamino\t  ya"}))
    assert salida.textos == ["Ana, tu pedido está en camino ya."]

    envio2 = await crear_envio(a.tid, conv)
    _, salida2 = await tocar(envio2, envio2.payload(), EntornoFalso(resultado={"estado": ["lista"]}))
    assert "lista" not in salida2.textos[0], "una lista no se cita"


# ---------------------------------------------------------------- firma, pertenencia, vigencia
async def test_firma_manipulada_o_conversacion_ajena_se_descarta_sin_responder(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    otra_conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso()
    bueno = envio.payload()

    manipulado = bueno[:-1] + ("0" if bueno[-1] != "0" else "1")
    for malo, conversacion_ in (
        (manipulado, None),
        ("opt:zzz", None),
        ("opt:" + "a" * 32 + ":ver:" + "b" * 16, None),   # envío inexistente
        (bueno, otra_conv),                                 # el botón de otra conversación
        (bueno.replace("opt:", "optc:", 1), None),          # el tipo no se cambia por el prefijo
    ):
        outcome, salida = await tocar(envio, malo, entorno, conversacion_=conversacion_)
        assert outcome in {"bad_signature", "unknown_send", "foreign_conversation"}
        assert salida.textos == [] and salida.confirmaciones == [], "sin señales para quien arma el webhook"
    assert entorno.llamadas == [] and await estado(envio) == []
    assert await auditoria(envio) == [], "lo descartado no ensucia la auditoría del negocio"


async def test_envio_vencido_y_version_retirada_responden_texto_fijo(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    entorno = EntornoFalso()

    vencido = await crear_envio(a.tid, conv, vence_en=timedelta(seconds=-1))
    outcome, salida = await tocar(vencido, vencido.payload(), entorno)
    assert (outcome, salida.textos) == ("expired", [texto("envio_vencido")])

    retirada = await crear_envio(a.tid, conv, retirada=True)
    outcome, salida = await tocar(retirada, retirada.payload(), entorno)
    assert (outcome, salida.textos) == ("retired", [texto("opcion_retirada")])

    assert entorno.llamadas == [] and await estado(vencido) == [] and await estado(retirada) == []


async def test_boton_viejo_ejecuta_su_version_mientras_no_venza(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    v1 = await crear_envio(a.tid, conv, definicion(ok="Versión uno, {ctx.cliente.nombre}."))
    await crear_envio(a.tid, conv, definicion(ok="Versión dos."))   # una versión posterior no cambia v1
    entorno = EntornoFalso()

    _, salida = await tocar(v1, v1.payload(), entorno)

    assert salida.textos == ["Versión uno, Ana."]


# ---------------------------------------------------------------- autorización y deriva
@pytest.mark.parametrize(
    ("cambios", "codigo"),
    [
        ({"apta_para_opcion": False}, "tool_no_apta"),
        ({"pack_activo": False}, "pack_inactivo"),
        ({"binding_habilitado": None}, "tool_sin_binding"),
        ({"binding_habilitado": False}, "tool_binding_deshabilitado"),
        ({"gobernada_por_procedimiento": True}, "tool_en_procedimiento"),
    ],
)
async def test_tool_denegada_no_ejecuta_y_responde_el_error(negocios, cambios, codigo) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(**cambios))

    outcome, salida = await tocar(envio, envio.payload(), entorno)

    assert outcome == "denied" and salida.textos == ["No pude consultarlo, Ana."]
    assert entorno.llamadas == [] and await estado(envio) == [], "una denegada no consume el envío"
    [fila] = await auditoria(envio)
    assert fila[3]["codigo"] == codigo


async def test_denegacion_del_loop_tambien_bloquea(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(denegacion="La herramienta está deshabilitada")

    outcome, _ = await tocar(envio, envio.payload(), entorno)

    assert outcome == "denied" and entorno.llamadas == []


async def test_deriva_de_schema_responde_el_error_y_no_ejecuta(negocios, caplog) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    # La tool cambió: ahora exige un argumento que la definición publicada no da.
    derivada = ficha(parameters={**PARAMETROS, "required": ["pedido", "nuevo"], "properties": {**PARAMETROS["properties"], "nuevo": {"type": "string"}}})
    entorno = EntornoFalso(derivada)

    outcome, salida = await tocar(envio, envio.payload(), entorno)

    assert outcome == "args_drift" and salida.textos == ["No pude consultarlo, Ana."]
    assert entorno.llamadas == [] and await estado(envio) == []
    # y la deriva por tipo: `pedido` pasó a ser un número
    entorno2 = EntornoFalso(ficha(parameters={"type": "object", "properties": {"pedido": {"type": "integer"}}, "required": ["pedido"]}))
    envio2 = await crear_envio(a.tid, conv)
    assert (await tocar(envio2, envio2.payload(), entorno2))[0] == "args_drift"


async def test_ruta_de_contexto_que_el_envio_no_resolvio_es_deriva(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv, definicion(args_contexto={"correo": "cliente.correo"}))
    entorno = EntornoFalso()

    outcome, _ = await tocar(envio, envio.payload(), entorno)

    assert outcome == "args_drift" and entorno.llamadas == []


async def test_argumentos_de_contexto_salen_del_envio_nunca_del_cliente(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(
        a.tid, conv, definicion(args_contexto={"correo": "cliente.correo"}),
        contexto={"cliente.nombre": "Ana", "cliente.correo": "ana@ejemplo.co"},
    )
    entorno = EntornoFalso()

    await tocar(envio, envio.payload(), entorno)

    assert entorno.llamadas[0][1] == {"pedido": "A-1", "correo": "ana@ejemplo.co"}


# ---------------------------------------------------------------- confirmación
def _ids(interactivo: dict[str, Any]) -> tuple[str, str]:
    [confirmar, cancelar] = interactivo["buttons"]
    return confirmar["id"], cancelar["id"]


async def test_opcion_sensible_pide_confirmacion_y_solo_optc_ejecuta(negocios) -> None:
    from core.kernel.opciones import payload
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(sensible=True))

    outcome, salida = await tocar(envio, envio.payload(), entorno)

    assert outcome == "confirm_requested" and entorno.llamadas == [] and salida.textos == []
    [interactivo] = salida.confirmaciones
    assert "A-1" in interactivo["body"], "el resumen lleva los argumentos exactos"
    assert [b["title"] for b in interactivo["buttons"]] == [texto("boton_confirmar"), texto("boton_cancelar")]
    optc, optn = _ids(interactivo)
    assert optc.startswith("optc:") and optn.startswith("optn:")
    leido = payload.leer(optc)
    assert leido is not None and payload.firma_valida(
        leido, payload.secreto_del_servidor(), a.tid, conv, payload.hash_de_argumentos(TOOL, {"pedido": "A-1"})
    ), "la firma de la confirmación se ata a los argumentos"
    assert await estado(envio) == [("ver", "pending_confirm", None)]

    # optc ejecuta, una sola vez aunque lo toquen dos veces a la vez
    resultados = await asyncio.gather(tocar(envio, optc, entorno), tocar(envio, optc, entorno))
    assert len(entorno.llamadas) == 1 and sorted(o for o, _ in resultados).count("ok") == 1
    assert await estado(envio) == [("ver", "done", None)]


async def test_optn_cancela_libera_la_opcion_y_un_optc_posterior_no_ejecuta(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(sensible=True))

    _, salida = await tocar(envio, envio.payload(), entorno)
    optc, optn = _ids(salida.confirmaciones[0])
    cancelado, s2 = await tocar(envio, optn, entorno)
    tarde, s3 = await tocar(envio, optc, entorno)

    assert cancelado == "cancelled" and s2.textos == [texto("cancelado")]
    assert tarde == "cancelled" and s3.textos == [texto("confirmacion_vencida")], "un optc tardío no ejecuta"
    assert entorno.llamadas == []
    assert [e[1] for e in await estado(envio)] == ["cancelled"]

    # cancelar libera la opción: el cliente puede volver a tocarla y confirmar
    _, s4 = await tocar(envio, envio.payload(), entorno)
    optc2, _ = _ids(s4.confirmaciones[0])
    assert (await tocar(envio, optc2, entorno))[0] == "ok" and len(entorno.llamadas) == 1


async def test_confirmacion_vencida_no_ejecuta_y_un_nuevo_toque_la_renueva(negocios) -> None:
    from core.kernel.opciones.resolver import VIGENCIA_CONFIRMACION
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(sensible=True))
    despues = lambda: datetime.now(UTC) + VIGENCIA_CONFIRMACION + timedelta(minutes=1)  # noqa: E731

    _, salida = await tocar(envio, envio.payload(), entorno)
    optc, _ = _ids(salida.confirmaciones[0])
    outcome, s2 = await tocar(envio, optc, entorno, ahora=despues)
    assert outcome == "expired" and s2.textos == [texto("confirmacion_vencida")] and entorno.llamadas == []

    # tocar otra vez la opción abre una confirmación nueva (la vencida ya se liberó)
    nuevo, s3 = await tocar(envio, envio.payload(), entorno)
    assert nuevo == "confirm_requested" and len(s3.confirmaciones) == 1


async def test_envio_exclusivo_con_la_confirmacion_vencida_de_otra_opcion_se_libera_para_la_nueva(negocios) -> None:
    from core.kernel.opciones.resolver import VIGENCIA_CONFIRMACION

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)  # exclusivo: una sola activación por envío
    entorno = EntornoFalso(ficha(sensible=True))
    despues = lambda: datetime.now(UTC) + VIGENCIA_CONFIRMACION + timedelta(minutes=1)  # noqa: E731

    await tocar(envio, envio.payload(), entorno)  # confirmación pendiente de «ver»
    outcome, salida = await tocar(envio, envio.payload(opcion="otra"), entorno, ahora=despues)

    assert outcome == "confirm_requested" and len(salida.confirmaciones) == 1, "la vencida de «ver» ya no bloquea a «otra»"
    assert sorted(estado_ for _, estado_, _ in await estado(envio)) == ["expired", "pending_confirm"]


async def test_optc_de_una_opcion_que_no_existe_se_descarta_sin_responder(negocios) -> None:
    from core.kernel.opciones import payload

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    fantasma = payload.construir(
        payload.secreto_del_servidor(), "optc", a.tid, conv, envio.send_id, "fantasma", payload.hash_de_argumentos(TOOL, {})
    )

    outcome, salida = await tocar(envio, fantasma, EntornoFalso(ficha(sensible=True)))

    assert outcome == "bad_signature" and salida.textos == [] and salida.confirmaciones == []
    assert await estado(envio) == []


async def test_tocar_de_nuevo_con_la_confirmacion_pendiente_la_reenvia_sin_ejecutar(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(sensible=True))

    await tocar(envio, envio.payload(), entorno)
    outcome, salida = await tocar(envio, envio.payload(), entorno)

    assert outcome == "confirm_requested" and len(salida.confirmaciones) == 1 and entorno.llamadas == []
    assert len(await estado(envio)) == 1


@pytest.mark.parametrize("cambios", [{"binding_pide_confirmacion": True}, {"sensible": True}])
async def test_binding_o_tool_sensible_obligan_la_confirmacion_aunque_la_definicion_no_la_pida(negocios, cambios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv, definicion(confirmar=False))
    entorno = EntornoFalso(ficha(**cambios))

    outcome, _ = await tocar(envio, envio.payload(), entorno)

    assert outcome == "confirm_requested" and entorno.llamadas == []


async def test_definicion_que_pide_confirmar_confirma_aunque_la_tool_no_sea_sensible(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv, definicion(confirmar=True))
    entorno = EntornoFalso()

    assert (await tocar(envio, envio.payload(), entorno))[0] == "confirm_requested"


async def test_optc_con_hash_de_otros_argumentos_se_descarta(negocios) -> None:
    from core.kernel.opciones import payload

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(sensible=True))
    await tocar(envio, envio.payload(), entorno)
    falso = envio.payload("optc", "ver", payload.hash_de_argumentos(TOOL, {"pedido": "OTRO"}))

    outcome, salida = await tocar(envio, falso, entorno)

    assert outcome == "bad_signature" and salida.textos == [] and entorno.llamadas == []


async def test_confirmacion_sin_botones_disponibles_avisa_que_no_se_hizo_nada(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    salida = Salida()
    salida.puede_confirmar = False

    outcome, salida = await tocar(envio, envio.payload(), EntornoFalso(ficha(sensible=True)), salida)

    assert outcome == "confirm_requested" and salida.textos == [texto("confirmacion_no_disponible")]
    # la pendiente no queda viva reteniendo la opción: el cliente puede volver a intentarlo
    assert await estado(envio) == [("ver", "cancelled", "confirmacion_no_disponible")]
    salida2 = Salida()
    otra, _ = await tocar(envio, envio.payload(), EntornoFalso(ficha(sensible=True)), salida2)
    assert otra == "confirm_requested" and len(salida2.confirmaciones) == 1, "no responde «confirmación pendiente»"


async def test_optc_repetido_tras_ejecutarse_no_responde_error_aunque_la_tool_ya_no_sea_elegible(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso(ficha(sensible=True))
    _, salida = await tocar(envio, envio.payload(), entorno)
    optc, _ = _ids(salida.confirmaciones[0])
    assert (await tocar(envio, optc, entorno))[0] == "ok"

    # el binding se deshabilita después de ejecutada: el optc repetido habla de lo que pasó, no de un error
    outcome, s2 = await tocar(envio, optc, EntornoFalso(ficha(sensible=True, binding_habilitado=False)))

    assert outcome == "duplicate" and s2.textos == [texto("ya_atendido")] and len(entorno.llamadas) == 1
    assert await estado(envio) == [("ver", "done", None)]


async def test_si_no_se_puede_registrar_el_fin_de_la_tool_no_dice_que_fallo_y_no_reejecuta(negocios, monkeypatch) -> None:
    from core.kernel.opciones import activaciones
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso()

    async def _roto(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("base caída")

    monkeypatch.setattr(activaciones, "terminar", _roto)
    outcome, salida = await tocar(envio, envio.payload(), entorno)
    monkeypatch.undo()

    assert outcome == "unconfirmed" and salida.textos == [texto("sin_confirmar")] and len(entorno.llamadas) == 1
    assert await estado(envio) == [("ver", "running", None)]
    assert (await tocar(envio, envio.payload(), entorno))[0] == "in_progress" and len(entorno.llamadas) == 1


async def test_resumen_que_no_se_puede_mostrar_entero_no_pide_confirmar_a_ciegas(negocios) -> None:
    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    largo = definicion()
    largo["opciones"][0]["accion"]["args_fijos"] = {"pedido": "x" * 300}
    envio = await crear_envio(a.tid, conv, largo)
    entorno = EntornoFalso(ficha(sensible=True))

    outcome, salida = await tocar(envio, envio.payload(), entorno)

    assert outcome == "denied" and salida.confirmaciones == [] and entorno.llamadas == []
    assert salida.textos == ["No pude consultarlo, Ana."] and await estado(envio) == []


# ---------------------------------------------------------------- worker caído
async def test_activacion_running_colgada_no_se_reejecuta(negocios) -> None:
    from core.kernel.opciones.textos import texto

    a, _ = negocios
    conv, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv)
    entorno = EntornoFalso()
    ahora = datetime.now(UTC)
    await _sql(
        a.tid,
        "insert into option_activations (id, tenant_id, send_id, option_id, state, dedupe_key, claimed_at) "
        "values (:i, :t, :s, 'ver', 'running', :k, :c)",
        i=uuid.uuid4(), t=a.tid, s=envio.send_id, k=envio.send_id.hex, c=ahora - timedelta(seconds=120),
    )

    outcome, salida = await tocar(envio, envio.payload(), entorno)
    assert (outcome, salida.textos) == ("unconfirmed", [texto("sin_confirmar")])

    # una reclamada hace menos de 60 s sigue «en curso»
    reciente = await crear_envio(a.tid, conv)
    await _sql(
        a.tid,
        "insert into option_activations (id, tenant_id, send_id, option_id, state, dedupe_key, claimed_at) "
        "values (:i, :t, :s, 'ver', 'running', :k, :c)",
        i=uuid.uuid4(), t=a.tid, s=reciente.send_id, k=reciente.send_id.hex, c=ahora - timedelta(seconds=5),
    )
    outcome, salida = await tocar(reciente, reciente.payload(), entorno)
    assert (outcome, salida.textos) == ("in_progress", [texto("en_curso")])
    assert entorno.llamadas == [], "nunca se reejecuta sola"


# ---------------------------------------------------------------- aislamiento entre negocios
async def test_payload_del_negocio_a_tocado_en_el_b_no_ejecuta_nada(negocios) -> None:
    a, b = negocios
    conv_a, _ = await conversacion(a.tid)
    conv_b, _ = await conversacion(b.tid)
    envio_a = await crear_envio(a.tid, conv_a)
    entorno = EntornoFalso()

    # (1) el payload tal cual: la firma ata el negocio
    outcome, salida = await tocar(envio_a, envio_a.payload(), entorno, tenant=b.tid, conversacion_=conv_b)
    assert outcome == "bad_signature" and salida.textos == []

    # (2) un payload FIRMADO para B pero con el send_id de A (quien tuviera la clave): la RLS no deja ver el envío
    from core.kernel.opciones import payload

    falsificado = payload.construir(payload.secreto_del_servidor(), "opt", b.tid, conv_b, envio_a.send_id, "ver")
    outcome, salida = await tocar(envio_a, falsificado, entorno, tenant=b.tid, conversacion_=conv_b)
    assert outcome == "unknown_send" and salida.textos == []

    assert entorno.llamadas == []
    assert await estado(envio_a) == [] and await _sql(b.tid, "select count(*) from option_activations") == [(0,)]


async def test_cargar_envio_bajo_otro_negocio_no_lo_ve(negocios) -> None:
    from core.kernel.opciones import activaciones
    from core.shared.tenant import tenant_scope

    a, b = negocios
    conv_a, _ = await conversacion(a.tid)
    envio = await crear_envio(a.tid, conv_a)

    async with tenant_scope(b.tid):
        assert await activaciones.cargar_envio(b.tid, envio.send_id) is None
        assert await activaciones.cargar_envio(a.tid, envio.send_id) is None, "ni con el tenant_id de A: manda la RLS"
    async with tenant_scope(a.tid):
        assert await activaciones.cargar_envio(a.tid, envio.send_id) is not None
