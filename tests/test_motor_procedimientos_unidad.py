"""HU-AOS-05 · motor de procedimientos: reglas puras, textos, sombra y registro (unitarios).

Sin BD ni red. Lo que toca la base (transacciones, consumer, outbox y loop) está en
`tests/integration/test_motor_procedimientos.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts import Procedure

from core.kernel.procedures import motor, sombra, textos
from core.kernel.procedures.linter import lint
from core.kernel.state import Evento, Proyeccion
from core.kernel.tarjeta import RotulosInvalidos

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "procedures"


def proc_fixture(nombre: str = "contratar_plan") -> Procedure:
    return Procedure.model_validate(json.loads((FIXTURES / f"{nombre}.json").read_text(encoding="utf-8")))


def proyeccion(stage: str = "elegir_plan", slots: dict[str, Any] | None = None, **kw: Any) -> Proyeccion:
    return Proyeccion(
        procedure_id="pack_commerce.contratar_plan", procedure_version="1.0.0", status="active", stage=stage,
        stack_position=0, last_seq=kw.pop("last_seq", 1), created_at=T0, updated_at=T0, slots=slots or {}, **kw,
    )


def ev(seq: int, tipo: str, **data: Any) -> Evento:
    return Evento(seq=seq, type=tipo, data=data, actor="kernel", created_at=T0)


def dato(valor: Any, *, confirmado: bool = False, fuente: str = "llm") -> dict[str, Any]:
    return {"value": valor, "source": fuente, "confirmed": confirmado, "data_class": "public"}


# ---------------------------------------------------------------- etapas y salida


def test_salida_por_datos_y_confirmados() -> None:
    proc = proc_fixture()
    datos = motor.etapa(proc, "datos")
    assert datos is not None
    assert not motor.salida_cumplida(proc, datos, proyeccion("datos"), [])
    sin_confirmar = proyeccion("datos", {"correo": dato("a@b.co")})
    assert not motor.salida_cumplida(proc, datos, sin_confirmar, [])
    confirmado = proyeccion("datos", {"correo": dato("a@b.co", confirmado=True)})
    assert motor.salida_cumplida(proc, datos, confirmado, [])
    assert motor.siguiente(proc, datos).id == "confirmar"  # type: ignore[union-attr]


def test_salida_por_accion_mira_solo_la_etapa_vigente() -> None:
    proc = proc_fixture()
    confirmar = motor.etapa(proc, "confirmar")
    assert confirmar is not None
    eventos = [
        ev(2, "action_succeeded", action="confirmar_cotizacion"),
        ev(3, "stage_entered", from_stage="datos", to_stage="confirmar"),
    ]
    assert not motor.salida_cumplida(proc, confirmar, proyeccion("confirmar"), motor.eventos_de_la_etapa(eventos))
    eventos.append(ev(4, "action_failed", action="confirmar_cotizacion"))
    assert not motor.salida_cumplida(proc, confirmar, proyeccion("confirmar"), motor.eventos_de_la_etapa(eventos))
    eventos.append(ev(5, "action_succeeded", action="confirmar_cotizacion"))
    assert motor.salida_cumplida(proc, confirmar, proyeccion("confirmar"), motor.eventos_de_la_etapa(eventos))


def test_required_when_que_no_aplica_cuenta_como_lleno() -> None:
    proc = proc_fixture("pedido_con_entrega")
    etapa = next(s for s in proc.stages if s.exitWhen and "direccion" in s.exitWhen.slots)
    metodo = proc.slots["direccion"].requiredWhen
    assert metodo is not None and etapa.exitWhen is not None
    otros = {n: dato("x", confirmado=True) for n in etapa.exitWhen.slots if n not in ("direccion", metodo.slot)}
    recoger = proyeccion(etapa.id, {**otros, metodo.slot: dato("recoger")})
    assert motor.salida_cumplida(proc, etapa, recoger, [])  # sin domicilio, la dirección no aplica
    domicilio = proyeccion(etapa.id, {**otros, metodo.slot: dato(metodo.equals)})
    assert not motor.salida_cumplida(proc, etapa, domicilio, [])
    con_dir = proyeccion(etapa.id, {**otros, metodo.slot: dato(metodo.equals), "direccion": dato("Cl 1")})
    assert not motor.salida_cumplida(proc, etapa, con_dir, [])  # falta confirmarla
    confirmada = proyeccion(
        etapa.id, {**otros, metodo.slot: dato(metodo.equals), "direccion": dato("Cl 1", confirmado=True)}
    )
    assert motor.salida_cumplida(proc, etapa, confirmada, [])


def test_faltan_lista_los_obligatorios_sin_valor() -> None:
    proc = proc_fixture()
    assert set(motor.faltan(proc, proyeccion())) == {"variante_id", "correo"}
    assert motor.faltan(proc, proyeccion(slots={"variante_id": dato("v1"), "correo": dato("a@b.co")})) == []


# ---------------------------------------------------------------- confirmación


def test_puerta_de_confirmacion_es_la_accion_de_salida() -> None:
    proc = proc_fixture()
    confirmar = motor.etapa(proc, "confirmar")
    assert confirmar is not None
    assert motor.requiere_confirmacion(confirmar, "confirmar_cotizacion")
    assert not motor.requiere_confirmacion(confirmar, "generar_cotizacion")
    assert not motor.puerta_de_salida(confirmar)  # una sola puerta: la acción


def test_confirmacion_atada_a_los_argumentos_y_se_consume() -> None:
    h_a, h_b = motor.hash_args({"quote_id": "A"}), motor.hash_args({"quote_id": "B"})
    assert h_a != h_b and motor.hash_args({"b": 1, "a": 2}) == motor.hash_args({"a": 2, "b": 1})
    objetivo = {"kind": "action", "name": "confirmar_cotizacion"}
    eventos = [ev(2, "confirmation_requested", target=objetivo, args_hash=h_a), ev(3, "confirmed", target=objetivo, args_hash=h_a)]
    assert motor.accion_confirmada(eventos, "confirmar_cotizacion", h_a)
    assert not motor.accion_confirmada(eventos, "confirmar_cotizacion", h_b)  # args distintos: no vale
    eventos.append(ev(4, "action_requested", action="confirmar_cotizacion", args_hash=h_a))
    assert not motor.accion_confirmada(eventos, "confirmar_cotizacion", h_a)  # ya se usó


def test_boton_de_confirmar_firmado_ida_y_vuelta() -> None:
    tid, negocio = "8f14e45f-ceea-467a-9b3f-1c2d3e4f5a6b", "e46e57e7-b3c5-4f5a-9112-0382b2365518"
    payload = motor.payload_boton_confirmar("secreto", negocio, tid, 7, "h1")
    assert payload.startswith(f"confirmar:{tid}:7:") and len(payload) < 256
    boton = motor.leer_boton_confirmar(payload)
    assert boton is not None and boton.task_id == tid and boton.seq == 7
    assert motor.boton_valido(boton, "secreto", negocio, "h1")
    assert not motor.boton_valido(boton, "secreto", negocio, "h2")  # otros argumentos
    assert not motor.boton_valido(boton, "secreto", "otro-negocio", "h1")
    assert not motor.boton_valido(boton, "otro-secreto", negocio, "h1")
    falso = motor.leer_boton_confirmar(f"confirmar:{tid}:8:{'0' * 16}")
    assert falso is not None and not motor.boton_valido(falso, "secreto", negocio, "h1")  # fabricado
    assert motor.leer_boton_confirmar(f"confirmar:{tid}:7") is None  # sin firma
    assert motor.dato_de_boton(proc_fixture(), payload) is None  # nunca llena un dato


def test_confirmacion_de_etapa_atada_a_los_valores() -> None:
    stage = proc_fixture().stages[1].model_copy(update={"confirmation": "explicit"})
    objetivo = {"kind": "stage", "name": stage.id}
    eventos = [ev(2, "confirmed", target=objetivo, args_hash="v1")]
    assert motor.etapa_confirmada(eventos, stage, "v1")
    assert not motor.etapa_confirmada(eventos, stage, "v2")  # un dato cambió después del sí


@pytest.mark.parametrize("texto", ["confirmo", "Sí, confirmo.", "sí, de acuerdo", "SI CONFIRMO!!", "correcto"])
def test_clasificador_alta_con_un_si_explicito(texto: str) -> None:
    assert textos.confirmacion_alta(texto)


@pytest.mark.parametrize("texto", ["dale", "ok", "listo", "sí", "si", "sí pero cámbialo", "no", "si quieres", "👍", ""])
def test_clasificador_no_confirma_lo_ambiguo(texto: str) -> None:
    assert not textos.confirmacion_alta(texto)


# ---------------------------------------------------------------- valores


@pytest.mark.parametrize(
    ("slot", "valor", "esperado"),
    [
        ({"type": "string", "format": "email"}, "Ana@Correo.CO", "ana@correo.co"),
        ({"type": "integer"}, "12", 12),
        ({"type": "number"}, "1,5", 1.5),
        ({"type": "date"}, "2026-01-05", "2026-01-05"),
        ({"enum": ["mensual", "anual"]}, "anual", "anual"),
        ({"type": "location"}, {"lat": 4.6, "lng": -74.1, "extra": "x"}, {"lat": 4.6, "lng": -74.1}),
        ({"type": "media"}, {"media_id": "m1", "mime": "image/jpeg"}, {"media_id": "m1", "mime": "image/jpeg"}),
    ],
)
def test_valores_validos_se_normalizan(slot: dict[str, Any], valor: Any, esperado: Any) -> None:
    from agentcore_contracts import Slot

    v, rechazo = motor.validar_valor(Slot.model_validate(slot), valor)
    assert rechazo is None and v == esperado


@pytest.mark.parametrize(
    ("slot", "valor", "codigo"),
    [
        ({"type": "string", "format": "email"}, "no-es-correo", "formato_invalido"),
        ({"type": "integer"}, True, "tipo_invalido"),
        ({"type": "integer"}, "doce", "tipo_invalido"),
        ({"type": "date"}, "mañana", "tipo_invalido"),
        ({"enum": ["mensual", "anual"]}, "semanal", "enum_invalido"),
        ({"type": "string"}, "   ", "tipo_invalido"),
        ({"type": "location"}, {"lat": 200, "lng": 0}, "tipo_invalido"),
    ],
)
def test_valores_invalidos_dan_un_codigo(slot: dict[str, Any], valor: Any, codigo: str) -> None:
    from agentcore_contracts import Slot

    v, rechazo = motor.validar_valor(Slot.model_validate(slot), valor)
    assert v is None and rechazo is not None and rechazo.codigo == codigo


# ---------------------------------------------------------------- entradas sin LLM y cierre


def test_boton_de_lista_llena_el_dato_de_su_prefijo() -> None:
    assert motor.dato_de_boton(proc_fixture(), "plan:premium-mensual") == ("variante_id", "premium-mensual")
    assert motor.dato_de_boton(proc_fixture(), "otro:x") is None
    assert motor.dato_de_boton(proc_fixture(), "plan:") is None


def test_tipo_de_contenido_por_mime() -> None:
    assert motor.tipo_de_contenido("image/jpeg") == "image"
    assert motor.tipo_de_contenido("audio/ogg") == "audio"
    assert motor.tipo_de_contenido("application/pdf") == "document"
    assert motor.tipo_de_contenido("video/mp4") is None  # no es un ContentKind


def test_candidatos_de_un_resultado_y_los_ultimos_ofrecidos() -> None:
    proc = Procedure.model_validate(
        {
            "id": "pack_calendar.agendar", "version": "1.0.0", "title": "Agendar",
            "slots": {"hora": {"type": "string", "setBy": "interactive:hora:",
                               "candidates": {"source": "action:consultar_disponibilidad.horarios", "max": 2}}},
            "stages": [{"id": "elegir", "actions": ["consultar_disponibilidad"], "exitWhen": {"slots": ["hora"]}}],
        }
    )
    res = {"horarios": [{"id": "10:00"}, "11:00", {"id": "12:00"}]}
    cands = motor.candidatos_de_resultado(proc, "consultar_disponibilidad", res)
    assert cands == {"hora": ["10:00", "11:00"]}
    eventos = [ev(2, "action_succeeded", action="consultar_disponibilidad", candidates=cands)]
    assert motor.ultimos_candidatos(eventos, "hora") == ["10:00", "11:00"]
    assert motor.ultimos_candidatos([], "hora") is None


def test_correlacion_solo_por_las_claves_del_topic_y_datos_del_servidor() -> None:
    from core.shared.events import CLAVES_DE_CORRELACION, ON_PAYMENT_CONFIRMED

    proc = Procedure.model_validate(
        {
            "id": "pack_commerce.pagar", "version": "1.0.0", "title": "Pagar",
            "slots": {"reference": {"type": "string", "setBy": "action:confirmar_pago_pedido"},
                      "amount": {"type": "number", "setBy": "action:confirmar_pago_pedido"},
                      "nota": {"type": "string"}},
            "stages": [{"id": "pagar", "actions": ["confirmar_pago_pedido"]}],
            "completion": {"events": ["core.payment.confirmed"]},
        }
    )
    claves = CLAVES_DE_CORRELACION[ON_PAYMENT_CONFIRMED]
    fuente = "action:confirmar_pago_pedido"
    servidor = proyeccion("pagar", {"reference": dato("OC-0042", fuente=fuente), "amount": dato(10, fuente=fuente)})
    assert motor.correlaciona(proc, servidor, {"reference": "OC-0042", "amount": 10}, claves)
    assert not motor.correlaciona(proc, servidor, {"reference": "OC-0001", "amount": 10}, claves)  # el monto no basta
    assert not motor.correlaciona(proc, servidor, {"customer_id": "OC-0042"}, claves)  # otra clave
    del_cliente = proyeccion("pagar", {"reference": dato("OC-0042", fuente="llm"), "nota": dato("OC-0042")})
    assert not motor.correlaciona(proc, del_cliente, {"reference": "OC-0042", "nota": "OC-0042"}, claves)
    assert not motor.correlaciona(proc, servidor, {"reference": "OC-0042"}, ())  # topic sin claves: nunca


# ---------------------------------------------------------------- textos


def test_playbooks_del_motor_cargan_completos() -> None:
    assert set(textos.MENSAJES) <= set(textos.mensajes())
    assert "{" not in textos.mensaje("fuera_de_etapa", accion="crear_pedido", etapa="datos")
    assert "si confirmo" in textos.lexico_si() and "si" not in textos.lexico_si()


def test_mensajes_incompletos_fallan() -> None:
    with pytest.raises(RotulosInvalidos):
        textos.parsear_mensajes("## fuera_de_etapa\nx {accion} {etapa}\n")
    roto = "\n".join(f"## {k}\nok" for k in textos.MENSAJES).replace("## sin_tarea\nok", "## sin_tarea\n{otro}")
    with pytest.raises(RotulosInvalidos):
        textos.parsear_mensajes(roto)
    with pytest.raises(RotulosInvalidos):
        textos.parsear_lexico("## si\n\n")


def test_error_de_resolver_queda_en_una_linea_acotada() -> None:
    assert textos.error_de_resolver("No existe\n## el plan " + "x" * 500).count("\n") == 0
    assert len(textos.error_de_resolver("x" * 900)) == 300


# ---------------------------------------------------------------- sombra (puro)


def _sombra(estado: dict[str, Any] | None, accion: str, args: dict[str, Any] | None = None, ok: bool = True,
            resultado: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, int]]:
    proc = proc_fixture()
    nuevo, difs = sombra.observar_puro({proc.id: proc}, frozenset({"remember_fact"}), estado, accion, args or {}, ok, resultado or {})
    return nuevo, dict(difs)


def test_sombra_empieza_con_una_accion_de_la_primera_etapa_y_no_guarda_valores() -> None:
    estado, difs = _sombra(None, "informar_plan", {"variante_id": "premium"})
    assert difs.get("inicio") == 1 and difs.get("transicion") == 1  # variante_id → sale de elegir_plan
    assert estado is not None and estado["etapa"] == "datos"
    assert "premium" not in json.dumps(estado)


def test_sombra_cuenta_acciones_fuera_de_etapa_y_sin_confirmacion() -> None:
    estado, _ = _sombra(None, "informar_plan")
    estado, difs = _sombra(estado, "crear_pedido")
    assert difs.get("accion_fuera_de_etapa") == 1
    estado = {**(estado or {}), "etapa": "confirmar", "acciones": []}
    _, difs = _sombra(estado, "confirmar_cotizacion")
    assert difs.get("accion_sin_confirmacion") == 1


def test_sombra_registra_dato_invalido_y_cierre() -> None:
    estado = {"pid": "pack_commerce.contratar_plan", "v": "1.0.0", "etapa": "datos", "llenos": [], "conds": {}, "acciones": []}
    _, difs = _sombra(dict(estado), "remember_fact", {"correo": "no"})
    assert difs == {}  # acción del core: sin diferencias
    _, difs = _sombra({**estado, "etapa": "elegir_plan"}, "informar_plan", {"correo": "no-es-correo"})
    assert difs.get("dato_invalido") == 1
    pagar = {**estado, "etapa": "pagar", "llenos": ["pedido_id"]}
    nuevo, difs = _sombra(pagar, "confirmar_pago_pedido", resultado={"link_pago": "https://pago.co/x"})
    assert nuevo is None and difs.get("cierre") == 1


def test_sombra_sin_tarea_y_accion_ambigua_no_hace_nada() -> None:
    assert _sombra(None, "accion_que_no_existe") == (None, {})
    assert _sombra(None, "informar_plan", ok=False) == (None, {})


# ---------------------------------------------------------------- linter y registro


def test_linter_reserva_el_prefijo_de_confirmar_y_el_dueno_de_los_eventos() -> None:
    raw = json.loads((FIXTURES / "contratar_plan.json").read_text(encoding="utf-8"))
    raw["triggers"]["interactivePayloadPrefix"] = "confirmar:"
    raw["completion"]["events"] = ["pack_cobranza.algo"]
    raw["slots"]["libre"] = {"type": "string", "setBy": "interactive:libre:"}
    issues = {i.code for i in lint(Procedure.model_validate(raw), {})}
    assert {"prefijo_reservado", "evento_de_habilidad_no_declarada", "dato_interactivo_sin_restriccion"} <= issues


def test_host_rechaza_procedimientos_ajenos_sin_tumbar_el_plugin() -> None:
    from core.mcp.host import MCPHost

    host = MCPHost()
    host.begin_activation("pack_commerce")
    ajeno = proc_fixture().model_copy(update={"id": "pack_calendar.x"})
    host.register_procedures([proc_fixture(), ajeno, proc_fixture()])  # el repetido tampoco entra
    host.end_activation()
    assert [p.id for p in host.get_procedures()["pack_commerce"]] == ["pack_commerce.contratar_plan"]
    host.remove_plugin("pack_commerce")
    assert host.get_procedures() == {}


def test_host_resolvers_solo_del_plugin() -> None:
    from core.mcp.host import MCPHost
    from core.mcp.types import ResolverResult
    from core.shared.exceptions import PluginError

    async def validar(valor: Any, ctx: Any, datos: dict[str, Any]) -> ResolverResult:
        return ResolverResult(ok=True)

    host = MCPHost()
    host.begin_activation("pack_commerce")
    host.register_resolver("pack_commerce.variante_de_plan", validar=validar)
    with pytest.raises(PluginError):
        host.register_resolver("pack_calendar.ajeno", validar=validar)
    with pytest.raises(PluginError):
        host.register_resolver("pack_commerce.vacio")
    host.end_activation()
    assert host.get_resolver("pack_commerce.variante_de_plan") is not None
    host.remove_plugin("pack_commerce")
    assert host.get_resolver("pack_commerce.variante_de_plan") is None


def test_registro_filtra_por_packs_habilitados_y_requires(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.kernel.procedures import registro
    from core.mcp.host import MCPHost

    host = MCPHost()
    host.begin_activation("pack_commerce")
    host.register_procedures([proc_fixture(), proc_fixture().model_copy(update={"id": "pack_commerce.otro", "requires": ["pack_calendar"]})])
    host.end_activation()
    monkeypatch.setattr(registro, "mcp_host", host)
    monkeypatch.setattr(registro, "_valido", lambda proc: True)
    assert set(registro.disponibles({"pack_commerce"})) == {"pack_commerce.contratar_plan"}
    assert set(registro.disponibles({"pack_commerce", "pack_calendar"})) == {"pack_commerce.contratar_plan", "pack_commerce.otro"}
    assert registro.disponibles({"pack_commerce"}, {"pack_commerce"}) == {}
    assert registro.definicion("pack_commerce.contratar_plan", "1.0.0") is not None
    assert registro.definicion("pack_commerce.contratar_plan", "2.0.0") is None  # versión que el binario ya no trae
    assert set(registro.con_evento("payment.confirmed")) == {"pack_commerce.contratar_plan", "pack_commerce.otro"}
    assert registro.con_evento("payment.rejected") == {}
