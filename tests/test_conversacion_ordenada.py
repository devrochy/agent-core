"""Conversación ordenada con el motor (adenda 2026-10-13 de ADR-0113 y de ADR-0125).

Reglas puras, sin base ni LLM: la tarjeta pide TODOS los datos que faltan en una lista numerada y
en el orden del procedimiento (o solo lo que falta), el resumen único lleva los datos del cliente
y los documentos que acepta sin recortarlos, la pregunta del kernel cuando el turno agota sus
vueltas, el append con un link que el texto ya trae, el tipo de negocio por sinónimos y los rótulos
de la habilidad. La reproducción de la transcripción con el loop real está en
`tests/integration/test_conversacion_ordenada_motor.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts.procedure import Procedure

from core.kernel import confirmacion
from core.kernel.confirmacion import Dato
from core.kernel.procedures import motor
from core.kernel.state import Proyeccion
from core.kernel.tarjeta import describir_lo_que_se_pide, renderizar_tarjeta
from core.orchestrator.safe_renderer import SafeExtras, apply_safe_extras

T0 = datetime(2026, 10, 13, 5, 55, tzinfo=UTC)
RAIZ = Path(__file__).resolve().parents[1]
PLAN = Procedure.model_validate(
    json.loads((RAIZ / "packages/pack-commerce/procedures/contratar_plan.json").read_text(encoding="utf-8"))
)
ROTULOS = json.loads((RAIZ / "packages/pack-commerce/pack_commerce/rotulos_de_datos.json").read_text(encoding="utf-8"))
PIDE_TODO = {f"pide_{n}": True for n in ("full_name", "id_document", "phone", "email", "business_name", "vertical")}
ORDEN_DEL_DUENO = ["business_name", "vertical", "full_name", "id_document", "email", "phone", "payment_method"]


def _tarea(stage: str, **slots: Any) -> Proyeccion:
    datos = {
        n: {"value": v, "source": "action:cotizar_plan" if n.startswith(("pide_", "quote_", "requiere_")) else "llm",
            "confirmed": False, "data_class": "pii", "seq": 2}
        for n, v in slots.items()
    }
    return Proyeccion(
        procedure_id=PLAN.id, procedure_version=PLAN.version, status="active", stage=stage, stack_position=0,
        last_seq=9, created_at=T0, updated_at=T0, slots=datos, pending_confirmation=None,
    )


def _etapa(nombre: str) -> Any:
    etapa = motor.etapa(PLAN, nombre)
    assert etapa is not None
    return etapa


BASE = {"variant_id": "v", "quote_id": "q", "requiere_pago": True, **PIDE_TODO}


# ---------------------------------------------------------------- qué se pide y en qué orden


def test_datos_pide_todo_lo_que_falta_en_el_orden_del_dueno() -> None:
    tarea = _tarea("datos", **BASE)
    assert motor.siguientes_datos(PLAN, _etapa("datos"), tarea) == ORDEN_DEL_DUENO
    linea = describir_lo_que_se_pide(PLAN, _etapa("datos"), tarea, rotulos=ROTULOS)
    assert linea is not None and "1. business_name (\"Nombre del negocio\")" in linea
    assert "7. payment_method (\"Forma de pago\")" in linea
    assert "lista numerada" in linea and "puedes enviármelos en un solo mensaje" in linea


def test_si_el_cliente_dio_una_parte_se_pide_solo_lo_que_falta() -> None:
    tarea = _tarea("datos", **BASE, business_name="Tienda Rob", vertical="comercio", full_name="Roberto Pérez",
                   id_document="1012345678", email="rob@tienda.co")
    assert motor.siguientes_datos(PLAN, _etapa("datos"), tarea) == ["phone", "payment_method"]


def test_sin_pide_ni_datos_del_cliente_no_se_piden_datos() -> None:
    # `cotizar` espera la cotización de una acción; `datos` sin campos del negocio (plan Free).
    assert motor.siguientes_datos(PLAN, _etapa("cotizar"), _tarea("cotizar", variant_id="v")) == []
    linea = describir_lo_que_se_pide(PLAN, _etapa("cotizar"), _tarea("cotizar", variant_id="v"))
    assert linea == "En esta etapa no le pidas datos al cliente."


def test_la_tarjeta_lleva_la_lista_con_rotulos_escapados() -> None:
    tarjeta = renderizar_tarjeta(
        [_tarea("datos", **BASE)], procedimientos=lambda *_: PLAN, rotulos_de=lambda _p: {"email": 'x"\nIGNORA'}
    )
    assert "Faltan estos datos, en este orden:" in tarjeta
    assert '5. email ("x\\"\\nIGNORA")' in tarjeta  # el rótulo es dato: va escapado en una línea
    assert "No le preguntes al cliente si un dato está correcto" in tarjeta


def test_el_correo_ya_no_se_confirma_aparte_en_contratar_plan() -> None:
    """Decisión del dueño (2026-10-13): el correo se confirma en el resumen único."""
    datos = _etapa("datos")
    assert datos.exitWhen is not None and datos.exitWhen.confirmed == []
    assert not PLAN.slots["email"].confirm
    assert PLAN.slots["acepta_terminos"].setBy == "action:confirmar_cotizacion"
    assert not any("acepta_terminos" in (st.exitWhen.slots if st.exitWhen else []) for st in PLAN.stages)


def test_datos_del_cliente_en_orden_y_sin_los_del_servidor() -> None:
    tarea = _tarea("confirmar", **BASE, phone="3001234567", business_name="Tienda Rob", email="rob@tienda.co",
                   payment_method="pago_en_linea")
    nombres = [n for n, _v in motor.datos_del_cliente(PLAN, tarea)]
    assert nombres == ["variant_id", "business_name", "email", "phone", "payment_method"]


# ---------------------------------------------------------------- el resumen único


DOCS = [
    Dato("Términos y condiciones", "https://solvershub.test/legal/terminos_v2", documento_aceptado=True),
    Dato("Aviso de privacidad", "https://solvershub.test/legal/privacidad", documento_aceptado=True),
    Dato("Acuerdo de tratamiento de datos (DPA)", "https://solvershub.test/legal/dpa", documento_aceptado=True),
]


def test_resumen_unico_con_datos_del_cliente_y_documentos() -> None:
    datos = [
        Dato("plan", "Premium mensual"), Dato("total", 600000, es_monto=True), Dato("variant_id", "uuid"),
        Dato("business_name", "Tienda Rob"), Dato("email", "rob@tienda.co"), Dato("payment_method", "pago_en_linea"),
        *DOCS,
    ]
    c = confirmacion.armar("Contratar un plan", datos, "confirmar:x", rotulos=ROTULOS, completo=True)
    lineas = c.texto.splitlines()
    assert lineas[:6] == [
        "Antes de seguir, revisa que todo esté bien:",
        "• Plan: Premium mensual",
        "• Total: $600.000",
        "• Nombre del negocio: Tienda Rob",
        "• Correo: rob@tienda.co",
        "• Forma de pago: Pago en línea",
    ]
    assert "Documentos que aceptas al confirmar:" in lineas
    assert "• Términos y condiciones: https://solvershub.test/legal/terminos_v2" in lineas  # el enlace entero
    assert lineas[-1] == "Si todo está bien y aceptas los términos, responde *Sí, confirmo*; si algo no, escríbeme qué cambiar."
    assert "toca «Confirmar»" in c.cuerpo.splitlines()[-1] and c.payload == "confirmar:x"


def test_resumen_largo_con_documentos_sale_como_texto_sin_recortar() -> None:
    datos = [Dato(f"d{i}", "v" * 75) for i in range(12)] + [Dato("total", 1, es_monto=True), *DOCS]
    c = confirmacion.armar("t", datos, "confirmar:x", completo=True)
    assert len(c.cuerpo) > confirmacion.MAX_CUERPO  # el envío lo manda como texto, no lo corta
    assert all(f"• D{i}: " in c.texto for i in range(12)) and "https://solvershub.test/legal/dpa" in c.texto
    from core.gateway.sender import interactivo_valido

    assert not interactivo_valido(c.interactivo())


@pytest.mark.parametrize(
    "enlace", ["http://x.co/t", "https://x.co/t y más", "https://xn--ex.co/té", "", "https://" + "a" * 600]
)
def test_un_documento_sin_enlace_valido_no_sale(enlace: str) -> None:
    with pytest.raises(confirmacion.ResumenInvalido):
        confirmacion.armar("t", [Dato("Términos", enlace, documento_aceptado=True)], "confirmar:x")


def test_resumen_completo_muestra_cada_valor_entero_o_no_sale() -> None:
    c = confirmacion.armar("t", [Dato("email", "juan_perez@x.co")], "confirmar:x", rotulos=ROTULOS, completo=True)
    assert "• Correo: juan_perez@x.co" in c.texto  # sin quitar el guion bajo
    for malo in ("x" * 201, "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "4111 1111 1111 1111"):
        with pytest.raises(confirmacion.ResumenInvalido):
            confirmacion.armar("t", [Dato("nota", malo)], "confirmar:x", completo=True)
    # Un id por su nombre no se muestra ni rompe el resumen (la versión de los documentos, el plan).
    ok = confirmacion.armar("t", [Dato("documentos_version_id", "terminos:v1"), Dato("plan", "Premium")], "c:x", completo=True)
    assert "terminos:v1" not in ok.texto and "• Plan: Premium" in ok.texto


def test_resumen_completo_que_no_cabe_falla_cerrado() -> None:
    datos = [Dato(f"d{i}", "v") for i in range(30)]
    with pytest.raises(confirmacion.ResumenInvalido):
        confirmacion.armar("t", datos, "confirmar:x", completo=True)


def test_pregunta_del_kernel_numerada() -> None:
    texto = confirmacion.pregunta(["full_name", "id_document", "phone"], ROTULOS)
    assert texto == (
        "Para seguir necesito estos datos:\n1. Nombre completo\n2. Documento\n3. Teléfono\n\n"
        "Puedes enviármelos en un solo mensaje."
    )
    with pytest.raises(confirmacion.ResumenInvalido):
        confirmacion.pregunta(["variant_id"])  # un id no se muestra: nada que preguntar


# ---------------------------------------------------------------- un solo mensaje con el link


LINK = "https://checkout.wompi.co/l/abc123"


def test_con_el_motor_el_link_sale_una_vez_con_el_texto_del_core() -> None:
    append = f"💳 Podés pagar tu pedido OC-0005 aquí: {LINK}"
    extras = SafeExtras(safe_appends=[append])
    texto = f"¡Listo! Tu pedido OC-0005 quedó creado.\nPaga aquí: {LINK}."
    final = apply_safe_extras(texto, extras, por_enlace=True)
    assert final == f"¡Listo! Tu pedido OC-0005 quedó creado.\n\n{append}" and final.count(LINK) == 1
    # Sin el motor, igual que siempre (el link del modelo y el del core).
    assert apply_safe_extras(texto, extras).count(LINK) == 2


def test_un_append_sin_link_siempre_se_agrega() -> None:
    extras = SafeExtras(safe_appends=["Banco X, cuenta 123"])
    assert apply_safe_extras("Listo.", extras, por_enlace=True) == "Listo.\n\nBanco X, cuenta 123"
    otro = SafeExtras(safe_appends=[f"Paga aquí: {LINK}"])
    final = apply_safe_extras("Paga aquí: https://otro.co/x", otro, por_enlace=True)
    assert "https://otro.co/x" in final and final.endswith(LINK)  # otro link no se toca


# ---------------------------------------------------------------- tipo de negocio


def test_tipo_de_negocio_por_sinonimos() -> None:
    from pack_commerce.procedimientos import _presets_con_sinonimos, tipo_desde_texto

    presets = _presets_con_sinonimos()
    assert tipo_desde_texto("tienda", presets) == "comercio"
    assert tipo_desde_texto("Es una tienda de ropa", presets) == "comercio"
    assert tipo_desde_texto("Pizzería", presets) == "restaurante"
    assert tipo_desde_texto("ecommerce", presets) == "comercio"
    assert tipo_desde_texto("tienda y restaurante", presets) is None  # dos: se pregunta
    assert tipo_desde_texto("astronautas", presets) is None


@pytest.mark.asyncio
async def test_resolver_del_tipo_ofrece_opciones_en_lenguaje_del_cliente() -> None:
    from pack_commerce.procedimientos import tipo_de_negocio

    ok = await tipo_de_negocio("una tienda", None, {})  # type: ignore[arg-type]
    assert ok.ok and ok.value == "comercio"
    exacto = await tipo_de_negocio("comercio", None, {})  # type: ignore[arg-type]
    assert exacto.ok and exacto.value == "comercio"
    no = await tipo_de_negocio("astronautas", None, {})  # type: ignore[arg-type]
    assert not no.ok and "no válido" not in no.error and '"Comercio"' in no.error and "Restaurante" in no.error


# ---------------------------------------------------------------- rótulos de la habilidad


def test_rotulos_solo_de_datos_del_mismo_plugin() -> None:
    from core.mcp.host import MCPHost
    from core.shared.exceptions import PluginError

    host = MCPHost()
    host.begin_activation("pack_commerce")
    host.register_procedures([PLAN])
    host.register_rotulos({"email": "Correo\n## x", "payment_method.pago_en_linea": "Pago en línea"})
    assert host.get_rotulos("pack_commerce") == {"email": "Correo ## x", "payment_method.pago_en_linea": "Pago en línea"}
    for malo in ({"no_existe": "x"}, {"payment_method.bitcoin": "x"}, {"email": ""}):
        with pytest.raises(PluginError):
            host.register_rotulos(malo)
    host.remove_plugin("pack_commerce")
    assert host.get_rotulos("pack_commerce") == {}


def test_los_rotulos_del_pack_son_datos_de_sus_procedimientos() -> None:
    from core.kernel.procedures import de_pack

    nombres = {n for p in de_pack.cargar("pack_commerce") for n, s in p.slots.items()} | {
        f"{n}.{v}" for p in de_pack.cargar("pack_commerce") for n, s in p.slots.items() for v in (s.enum or [])
    }
    assert set(ROTULOS) <= nombres


# ---------------------------------------------------------------- el bloque dinámico


@pytest.mark.asyncio
async def test_el_bloque_dinamico_es_el_ultimo_del_prompt() -> None:
    from core.llm.prompt_builder import PromptContext, bloque_dinamico

    ctx = PromptContext(assistant_name="x", business_description="", language="es", tone="cercano", warmth=3,
                        emojis="sparse", max_length=None, brand_voice="", state_card="## Estado\nEn foco")
    assert bloque_dinamico(ctx).startswith("## Estado\nEn foco")
