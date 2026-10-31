"""Confirmaciones de datos visibles y "sí" a secas para datos (adenda 2026-10-12 de ADR-0113).

Reglas puras: qué bloquea la salida de una etapa (`motor.faltas_de_salida`), qué dato pide el kernel
(`motor.dato_por_confirmar`), si el cliente escribió un dato personal (`motor.dicho_por_el_cliente`),
el asentimiento para un dato (`clasificador.es_asentimiento`, medido con
`tests/fixtures/confirmacion/dato.jsonl`), el criterio único `textos.confirmacion_suficiente`, la línea
de la tarjeta y el mensaje del kernel para un dato. Sin base ni LLM.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts.procedure import Procedure

from core.kernel import confirmacion
from core.kernel.procedures import clasificador, motor, textos
from core.kernel.state import Proyeccion
from core.kernel.tarjeta import describir_faltas, renderizar_tarjeta

T0 = datetime(2026, 10, 12, 23, 45, tzinfo=UTC)
RAIZ = Path(__file__).resolve().parents[1]
def _plan_con_correo_confirmado() -> Procedure:
    """`contratar_plan` con la confirmación aparte del correo en `datos`, como en 1.1.0: el
    mecanismo del kernel sigue para los procedimientos que confirman un dato por separado. Desde
    1.2.0 (adenda 2026-10-13) el plan lo confirma dentro del resumen único."""
    crudo = json.loads((RAIZ / "tests/fixtures/procedures_congeladas/contratar_plan_comercio.json").read_text(encoding="utf-8"))
    for etapa in crudo["stages"]:
        if etapa["id"] == "datos":
            etapa["exitWhen"]["confirmed"] = ["email"]
    return Procedure.model_validate(crudo)


PLAN = _plan_con_correo_confirmado()
_SET_DATO = Path(__file__).parent / "fixtures" / "confirmacion" / "dato.jsonl"


def _tarea(stage: str, pendiente: dict[str, Any] | None = None, **slots: Any) -> Proyeccion:
    datos = {
        n: {"value": v[0] if isinstance(v, tuple) else v, "source": "llm", "confirmed": isinstance(v, tuple) and v[1],
            "data_class": "pii", "seq": 2}
        for n, v in slots.items()
    }
    return Proyeccion(
        procedure_id=PLAN.id, procedure_version=PLAN.version, status="active", stage=stage, stack_position=0,
        last_seq=9, created_at=T0, updated_at=T0, slots=datos, pending_confirmation=pendiente,
    )


def _etapa(nombre: str) -> Any:
    etapa = motor.etapa(PLAN, nombre)
    assert etapa is not None
    return etapa


# El estado del incidente en test: `datos` con todos los datos, correo sin confirmar, nada pendiente.
DATOS_COMPLETOS: dict[str, Any] = {
    "variant_id": "v", "quote_id": "q", "requiere_pago": True, "pide_full_name": True, "pide_id_document": True,
    "pide_phone": True, "pide_email": True, "pide_business_name": True, "pide_vertical": True,
    "full_name": "Rob", "id_document": "1012345678", "phone": "3001234567", "email": "rob@tienda.co",
    "business_name": "Tienda", "vertical": "restaurante", "payment_method": "pago_en_linea",
}


# ---------------------------------------------------------------- qué bloquea la etapa


def test_el_estado_del_incidente_dice_que_falta_confirmar_el_correo() -> None:
    tarea = _tarea("datos", **DATOS_COMPLETOS)
    faltas = motor.faltas_de_salida(PLAN, _etapa("datos"), tarea)
    assert faltas == [motor.Falta("confirmar_dato", "email")]
    assert motor.dato_por_confirmar(PLAN, _etapa("datos"), tarea) == "email"
    assert "que el cliente confirme el dato email" in describir_faltas(PLAN, _etapa("datos"), tarea)


def test_el_correo_no_se_pide_mientras_falten_otros_datos() -> None:
    """Una pregunta a la vez: el correo se confirma cuando es lo único que falta."""
    sin_telefono = {k: v for k, v in DATOS_COMPLETOS.items() if k != "phone"}
    tarea = _tarea("datos", **sin_telefono)
    faltas = motor.faltas_de_salida(PLAN, _etapa("datos"), tarea)
    assert motor.Falta("dato_cliente", "phone") in faltas and motor.Falta("confirmar_dato", "email") in faltas
    assert motor.dato_por_confirmar(PLAN, _etapa("datos"), tarea) is None


def test_con_otra_confirmacion_pendiente_no_se_pide_el_dato() -> None:
    pendiente = {"kind": "action", "name": "confirmar_cotizacion", "seq": 9}
    assert motor.dato_por_confirmar(PLAN, _etapa("datos"), _tarea("datos", pendiente, **DATOS_COMPLETOS)) is None


def test_un_dato_confirmado_no_bloquea_y_la_etapa_de_accion_nombra_la_accion() -> None:
    confirmado = {**DATOS_COMPLETOS, "email": ("rob@tienda.co", True)}
    assert motor.faltas_de_salida(PLAN, _etapa("datos"), _tarea("datos", **confirmado)) == []
    assert motor.faltas_de_salida(PLAN, _etapa("confirmar"), _tarea("confirmar", **confirmado)) == [
        motor.Falta("accion", "confirmar_cotizacion")
    ]


def test_los_datos_del_servidor_y_los_que_no_aplican() -> None:
    # `cotizar`: falta la cotización, que fija una acción (no se le pide al cliente).
    tarea = _tarea("cotizar", variant_id="v")
    assert motor.faltas_de_salida(PLAN, _etapa("cotizar"), tarea) == [motor.Falta("dato_accion", "quote_id")]
    # Sin `pide_*`, los datos condicionales no aplican: no bloquean.
    solo = _tarea("datos", variant_id="v", quote_id="q", requiere_pago=False)
    assert motor.faltas_de_salida(PLAN, _etapa("datos"), solo) == []


def test_la_tarjeta_lleva_la_linea_de_lo_que_falta() -> None:
    tarjeta = renderizar_tarjeta([_tarea("datos", **DATOS_COMPLETOS)], procedimientos=lambda *_: PLAN)
    assert "Para pasar de esta etapa falta: que el cliente confirme el dato email" in tarjeta
    assert "no digas que te falta una herramienta" in tarjeta
    # Sin definición del procedimiento no se inventa nada.
    assert "Para pasar" not in renderizar_tarjeta([_tarea("datos", **DATOS_COMPLETOS)])


# ---------------------------------------------------------------- lo que dijo el cliente


@pytest.mark.parametrize(
    ("valores", "textos_cliente", "telefonos", "dicho"),
    [
        (["Rob"], ["Hola, quiero el plan premium", "1012345678, 3001234567"], [], False),  # el perfil del canal
        (["Rob"], ["Soy Roberto"], [], False),  # "Rob" no sale de "Roberto"
        (["Roberto Pérez"], ["roberto perez"], [], True),
        (["Pérez Roberto"], ["Roberto Pérez"], [], True),  # el orden no importa
        (["Roberto Pérez Gómez"], ["Roberto Pérez"], [], False),  # no se agregan palabras
        (["1012345678"], ["mi cédula es 1.012.345.678"], [], True),
        (["+573001234567"], ["cel 300 123 4567"], [], True),  # el modelo agregó el indicativo
        (["3001234567"], ["hola"], ["+57 300 123 4567 whatsapp:+573001234567"], True),  # teléfono del canal
        (["3001234567"], ["mi cédula es 1012345678"], [], False),
        (["Ana@Correo.co", "ana@correo.co"], ["mi correo es Ana@Correo.co"], [], True),
        (["ana@correo.co"], ["ana", "correo co"], [], False),  # un correo va entero en un mensaje
        (["Calle 1 # 2-3"], ["Calle 1 # 2-3, Bogotá"], [], True),
        ([None, True], ["hola"], [], False),
        ([{"nombre": "Ana"}], ["Ana"], [], False),  # un objeto nunca cuenta como dicho
        (["9912345678"], ["pedido 12345678"], [], False),  # sufijo corto: no es un teléfono
        (["5712345678"], ["mi pedido es el 2345678"], [], False),
        (["12345678"], ["mi cédula es 1012345678"], [], False),  # un fragmento no cuenta
        (["1012345678"], ["1012345678 3001234567"], [], True),  # dos números seguidos
        (["573001234567"], ["+57 300 123 4567"], [], True),
    ],
)
def test_dicho_por_el_cliente(valores: list[Any], textos_cliente: list[str], telefonos: list[str], dicho: bool) -> None:
    assert motor.dicho_por_el_cliente(valores, textos_cliente, telefonos) is dicho


# ---------------------------------------------------------------- sí a secas para un dato


def _set_dato() -> list[tuple[str, bool]]:
    filas = [json.loads(linea) for linea in _SET_DATO.read_text(encoding="utf-8").splitlines() if linea.strip()]
    return [(f["texto"], bool(f["asiente"])) for f in filas]


def test_set_de_datos_precision_total_y_recall_reportado() -> None:
    filas = _set_dato()
    assert len(filas) >= 40
    falsos_si = [t for t, etiqueta in filas if clasificador.es_asentimiento(t) and not etiqueta]
    assert falsos_si == [], f"falsos positivos: {falsos_si}"
    positivos = [t for t, etiqueta in filas if etiqueta]
    aciertos = sum(clasificador.es_asentimiento(t) for t in positivos)
    print(f"recall del asentimiento de datos: {aciertos / len(positivos):.2%} ({aciertos}/{len(positivos)})")
    assert aciertos / len(positivos) >= 0.9


@pytest.mark.parametrize("texto", ["Si", "sí", "ok", "dale", "Sí, confirmo el correo", "Si, el correo está bien"])
def test_un_dato_acepta_el_si_a_secas_y_una_accion_no(texto: str) -> None:
    assert textos.confirmacion_suficiente(texto, kind="slot", data_class="pii")
    # Lo que compromete sigue estricto (HU-AOS-09), también con objetos de dato en la frase.
    assert not textos.confirmacion_suficiente(texto, kind="action")
    assert not textos.confirmacion_suficiente(texto, kind="stage")


@pytest.mark.parametrize("clase", ["payment", "public", None])
def test_un_dato_que_no_es_pii_sigue_estricto(clase: str | None) -> None:
    """Lista permitida (revisión de seguridad): un dato `public` puede llevar monto o efecto."""
    assert not textos.confirmacion_suficiente("sí", kind="slot", data_class=clase)
    assert textos.confirmacion_suficiente("Sí, confirmo", kind="slot", data_class=clase)


def test_el_lexico_no_admite_negaciones_dentro_de_una_frase_permitida() -> None:
    from core.agents.playbooks import load_core_playbook
    from core.kernel.tarjeta import RotulosInvalidos

    contenido = load_core_playbook("confirmacion").replace("## asiente_dato\n", "## asiente_dato\nno hay problema\n")
    with pytest.raises(RotulosInvalidos):
        clasificador.parsear_lexico(contenido)


def test_los_objetos_de_dato_no_suben_el_recall_de_las_acciones() -> None:
    """`objetos_de_dato` y `asiente_dato` solo los lee el asentimiento: "confirmo el correo" nunca
    confirma una acción con monto (revisión del arquitecto)."""
    lx = clasificador.lexico()
    assert "el correo" in lx.objetos_de_dato and "el correo" not in lx.permitidas
    assert not clasificador.es_confirmacion("confirmo el correo")
    assert clasificador.es_asentimiento("confirmo el correo")


def test_cortesia_intercalada_en_una_frase_exacta() -> None:
    assert clasificador.es_confirmacion("Sí, está bien, gracias")
    assert clasificador.es_confirmacion("sí, es correcto, por favor")
    assert not clasificador.es_confirmacion("sí, gracias")  # la cortesía nunca suma núcleo
    assert not clasificador.es_confirmacion("sí, todo bien")


# ---------------------------------------------------------------- el mensaje del kernel para un dato


def test_el_mensaje_de_un_dato_es_texto_y_basta_un_si() -> None:
    c = confirmacion.armar("Contratar un plan", [confirmacion.Dato("email", "rob@tienda.co")], "confirmar:x", es_dato=True)
    assert c.payload == "" and c.titulo_boton == "" and c.cuerpo == c.texto
    assert "Email: rob@tienda.co" in c.texto and "responde «sí»" in c.texto
    accion = confirmacion.armar("Contratar un plan", [confirmacion.Dato("email", "rob@tienda.co")], "confirmar:x")
    assert accion.payload == "confirmar:x" and "*Sí, confirmo*" in accion.texto


def test_los_mensajes_nuevos_del_motor_cargan() -> None:
    assert "no aparece en lo que escribió el cliente" in textos.mensaje("dato_no_dicho", dato="full_name")
    falta = textos.mensaje("fuera_de_etapa_falta", accion="crear_pedido", etapa="datos", faltas="x")
    assert "Para pasar de esta etapa falta: x" in falta
    assert "crear_pedido" in textos.mensaje("confirmacion_vencida", accion="crear_pedido")
    assert textos.mensaje("confirmacion_repedida")


# ---------------------------------------------------------------- el hook al empezar el turno


async def test_al_empezar_turno_en_sombra_o_sin_nada_pendiente_no_toca_la_base(monkeypatch: Any) -> None:
    from core.kernel.procedures import ejecucion

    def _prohibido(*_: Any, **__: Any) -> Any:
        raise AssertionError("no debe abrir una transacción")

    monkeypatch.setattr(ejecucion, "_con_lock", _prohibido)
    comunes: dict[str, Any] = {
        "tenant_id": uuid.uuid4(), "conversation_id": uuid.uuid4(), "customer_id": uuid.uuid4(),
        "channel": "whatsapp", "procedimientos": {}, "herramientas_base": [], "candidatas": [],
    }
    await ejecucion.ContextoMotor(modo="sombra", **comunes).al_empezar_turno()
    await ejecucion.ContextoMotor(modo="activo", **comunes).al_empezar_turno()  # sin foco
    foco = ejecucion._Foco(
        fila=None,  # type: ignore[arg-type]
        tarea=_tarea("datos", variant_id="v"),
        proc=PLAN,
        stage=_etapa("datos"),
    )
    await ejecucion.ContextoMotor(modo="activo", foco=foco, **comunes).al_empezar_turno()  # nada que pedir
