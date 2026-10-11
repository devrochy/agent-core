"""HU-AOS-06 · el verificador usa el estado de las tareas como evidencia (ADR-0119).

Gherkin: dada una tarea con `pedido_id`, "tu pedido OC-0042 quedó creado" pasa y, si cita
otro número, falla. Más los casos que pidió la revisión del agente `arquitecto`: la segunda
compra del episodio no queda respaldada por la primera, lo que escribió el modelo no es
evidencia, las referencias vigentes (ADR-0130) cuentan, un SKU de otra familia sigue en
sombra y, sin estado (bandera apagada), el veredicto es idéntico, conteos de sombra incluidos.
Puro: sin BD ni red.
"""

from __future__ import annotations

import re
from typing import Any

from core.mcp.types import Afirmacion
from core.orchestrator.verificador import EstadoTareas, Evidencia, verificar, verificar_seguro

# Una afirmación como la que registra pack-commerce (tools = {crear_pedido}).
CREADO = Afirmacion(
    label="pedido_creado",
    patron=re.compile(r"pedido\b(?:\W+[\w-]+){0,3}\W+(?:qued[óo]\s+)?cread\w*", re.IGNORECASE),
    tools=frozenset({"crear_pedido"}),
)


def _tarea(*, cierre: bool = True, status: str = "active", **slots: tuple[Any, str]) -> dict[str, Any]:
    return {
        "procedure_id": "pack_commerce.contratar_plan",
        "status": status,
        "cierre": cierre,
        # Como `instantaneas_de_tareas`: solo `pedido_id` lo declara una afirmación del procedimiento.
        "slots": {
            n: {"value": v, "source": fuente, "respalda_cierre": n == "pedido_id"} for n, (v, fuente) in slots.items()
        },
    }


def _con_estado(texto: str, *tareas: dict[str, Any], resultados: list[dict] | None = None, vigentes=()):
    estado = EstadoTareas(tareas=tuple(tareas), referencias_vigentes=tuple(vigentes))
    return verificar(texto, Evidencia.desde(resultados or [], "", estado), [CREADO])


PEDIDO_42 = _tarea(pedido_id=("OC-0042", "action:crear_pedido"))


# ---------------------------------------------------------------- Gherkin


def test_el_numero_del_pedido_de_la_tarea_pasa() -> None:
    v = _con_estado("¡Listo! Tu pedido OC-0042 quedó creado.", PEDIDO_42)
    assert v.passed, v.motivo
    assert v.conteo["cierres"] == 1 and v.conteo["referencias"] == 1


def test_otro_numero_falla() -> None:
    v = _con_estado("¡Listo! Tu pedido OC-0099 quedó creado.", PEDIDO_42)
    assert not v.passed and v.motivo == "referencia_sin_evidencia"
    assert v.codigo == "verificador.referencia_sin_evidencia"


def test_sin_estado_es_identico_a_antes() -> None:
    """Bandera apagada: el cierre exige la acción del turno y las referencias van a sombra."""
    texto = "Tu pedido OC-0099 quedó creado."
    sin = verificar(texto, Evidencia.desde([], ""), [CREADO])
    seguro = verificar_seguro(texto, [], "", [CREADO])
    assert not sin.passed and sin.motivo == "status_sin_evidencia"
    assert sin.sombra == {"referencias_sin_evidencia": 1} == seguro.sombra
    assert sin.conteo == seguro.conteo and seguro.motivo == sin.motivo


# ---------------------------------------------------------------- alcance del estado


def test_la_compra_anterior_del_episodio_no_respalda_la_siguiente() -> None:
    """La tarea 1 se cerró antes de la última respuesta: su `pedido_id` no respalda el cierre de
    la tarea 2 (en foco, aún sin pedido). Su número sí es evidencia de referencia."""
    anterior = _tarea(cierre=False, status="completed", pedido_id=("OC-0042", "action:crear_pedido"))
    actual = _tarea(correo=("ana@correo.co", "llm"))
    v = _con_estado("Tu pedido quedó creado.", anterior, actual)
    assert not v.passed and v.motivo == "status_sin_evidencia"
    assert _con_estado("Tu compra anterior fue la OC-0042.", anterior, actual).passed


def test_una_tarea_completada_despues_de_la_ultima_respuesta_respalda() -> None:
    cerrada = _tarea(cierre=True, status="completed", pedido_id=("OC-0042", "action:crear_pedido"))
    assert _con_estado("Tu pedido OC-0042 quedó creado.", cerrada).passed


def test_lo_que_escribio_el_modelo_no_es_evidencia() -> None:
    """Un dato con origen `llm` (o de un botón) no respalda montos, referencias ni cierres."""
    del_modelo = _tarea(pedido_id=("OC-0042", "llm"), total=(600000, "llm"))
    v = _con_estado("Tu pedido OC-0042 quedó creado.", del_modelo)
    assert not v.passed and v.motivo == "status_sin_evidencia"
    monto = _con_estado("El total es $600.000.", del_modelo)
    assert not monto.passed and monto.motivo == "monto_sin_evidencia"


def test_los_montos_del_servidor_en_la_tarea_son_evidencia() -> None:
    cotizada = _tarea(cotizacion_id=("COT-0007", "action:generar_cotizacion"), total=(600000, "action:generar_cotizacion"))
    assert _con_estado("El total de tu cotización COT-0007 es $600.000.", cotizada).passed


# ---------------------------------------------------------------- referencias: falsos positivos


def test_solo_se_exige_la_familia_que_fijo_el_servidor() -> None:
    """Un SKU o un código de plan con forma de referencia sigue en sombra."""
    v = _con_estado("Te recomiendo el SKU-1234, y tu pedido OC-0042 quedó creado.", PEDIDO_42)
    assert v.passed
    assert v.sombra["referencias_sin_evidencia"] == 1


def test_sin_un_dato_de_referencia_en_las_tareas_todo_queda_en_sombra() -> None:
    sin_referencia = _tarea(correo=("ana@correo.co", "llm"))
    assert _con_estado("Tu pedido OC-0099 está en camino.", sin_referencia).passed
    assert _con_estado("Tu pedido OC-0099 está en camino.").passed  # tareas = []


def test_preguntas_negaciones_y_condicionales_no_afirman() -> None:
    for texto in (
        "¿Tu pedido es el OC-0099?",
        "No encuentro el pedido OC-0099.",
        "Para revisar el OC-0099 necesito tu correo.",
    ):
        assert _con_estado(texto, PEDIDO_42).passed, texto


def test_las_referencias_vigentes_cuentan() -> None:
    vigentes = ["- pedido: reference=OC-0001, estado=pagado"]
    assert _con_estado("Tu pedido OC-0001 ya está pagado.", PEDIDO_42, vigentes=vigentes).passed


def test_un_resultado_de_accion_cuenta_y_lo_que_cito_el_cliente_no() -> None:
    resultado = [{"_tool_name": "consultar_estado_pedido", "_current_turn": False, "_ok": True, "reference": "OC-0003"}]
    assert _con_estado("Tu pedido OC-0003 está en camino.", PEDIDO_42, resultados=resultado).passed
    # El número que escribió el cliente no es dato del servidor: hay que consultarlo.
    v = verificar(
        "Tu pedido OC-0077 está en camino.",
        Evidencia.desde([], "mi pedido es el OC-0077", EstadoTareas(tareas=(PEDIDO_42,))),
        [CREADO],
    )
    assert not v.passed and v.motivo == "referencia_sin_evidencia"


def test_el_monto_manda_sobre_la_referencia() -> None:
    v = _con_estado("Tu pedido OC-0099 por $1.234 quedó creado.", PEDIDO_42)
    assert v.motivo == "monto_sin_evidencia"


async def test_instruccion_de_reintento_desde_el_playbook_del_kernel(monkeypatch) -> None:
    """La sección no está en `respuestas` (cambiaría lo que el modelo puede leer con las banderas
    apagadas): sale del playbook del kernel, y un negocio puede reemplazarla en `respuestas`."""
    from core.orchestrator.verificador import Veredicto, instruccion_correctiva

    async def sin_seccion(_spec, _clave):
        return None

    monkeypatch.setattr("core.agents.responses.load_response", sin_seccion)
    texto = await instruccion_correctiva(None, Veredicto(texto="", passed=False, motivo="referencia_sin_evidencia"))
    assert "número de pedido o referencia" in texto

    async def del_negocio(_spec, clave):
        return "Texto del negocio." if clave == "verificador_referencia_sin_evidencia" else None

    monkeypatch.setattr("core.agents.responses.load_response", del_negocio)
    veredicto = Veredicto(texto="", passed=False, motivo="referencia_sin_evidencia")
    assert await instruccion_correctiva(None, veredicto) == "Texto del negocio."


def test_respuestas_del_core_no_cambia() -> None:
    """El playbook `respuestas` entra al prompt de las evals grabadas: no lleva la sección nueva."""
    from pathlib import Path

    contenido = (Path(__file__).parents[1] / "core" / "playbooks" / "respuestas.md").read_text(encoding="utf-8")
    assert "referencia_sin_evidencia" not in contenido


def test_un_dato_que_ninguna_afirmacion_declara_no_respalda_cierres() -> None:
    """Una lectura que fijó un dato (`estado = pendiente` por `crear_pedido`, en este ejemplo) no
    respalda el cierre en los turnos siguientes: solo los datos de `claims[].requires/mustMatch`."""
    lectura = _tarea(estado=("pendiente", "action:crear_pedido"))
    v = _con_estado("Tu pedido quedó creado.", lectura)
    assert not v.passed and v.motivo == "status_sin_evidencia"
