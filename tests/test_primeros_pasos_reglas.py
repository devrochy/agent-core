"""Reglas de los primeros pasos (HU-SH-06, ADR-0124): cada paso sale del estado real.

Funciones puras: reciben lo que `calculo.py` leyó de la base y deciden el estado, el motivo y
cuándo se cumplió. La lectura (y el aislamiento entre negocios) la prueba
`tests/integration/test_primeros_pasos.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.primeros_pasos.contrato import ContenidoCargado
from core.primeros_pasos.reglas import (
    ConexionMeta,
    Habilidad,
    RequisitoDePreset,
    SesionWaweb,
    contar,
    numero_verificado,
    paso_activar_preset,
    paso_catalogo,
    paso_datos_negocio,
    paso_equipo,
    paso_probar_asistente,
    paso_whatsapp,
    pasos_de_habilidades,
    segundos_desde_alta,
)

AHORA = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
NUMERO_OK = {"number_registered": {"ok": True, "detail": "status=CONNECTED"}}


# ------------------------------------------------------------------ datos del negocio
def test_datos_del_negocio_se_cumplen_con_la_descripcion() -> None:
    paso = paso_datos_negocio("Panadería de barrio en Cali", AHORA)
    assert (paso.clave, paso.estado, paso.completado_en, paso.seccion) == (
        "datos_negocio",
        "completo",
        AHORA,
        "settings",
    )


@pytest.mark.parametrize("descripcion", [None, "", "   \n "])
def test_sin_descripcion_el_paso_queda_pendiente(descripcion: str | None) -> None:
    paso = paso_datos_negocio(descripcion, AHORA)
    assert (paso.estado, paso.motivo, paso.completado_en) == ("pendiente", "sin_descripcion", None)


# ------------------------------------------------------------------ WhatsApp
def test_numero_verificado_mira_todos_los_numeros_de_la_conexion() -> None:
    assert numero_verificado(NUMERO_OK)
    assert numero_verificado({"number_registered:123": {"ok": True}})
    assert not numero_verificado({"number_registered": {"ok": None}})
    assert not numero_verificado({"number_registered": {"ok": False}, "token_valid": {"ok": True}})
    assert not numero_verificado({"number_registered": "ok"})  # un dato raro no cuenta
    assert not numero_verificado(None)


def test_whatsapp_conectado_y_verificado_se_cumple_con_la_hora_de_la_conexion() -> None:
    antes = AHORA - timedelta(hours=3)
    paso = paso_whatsapp(
        [
            ConexionMeta(status="connected", health=NUMERO_OK, conectada_en=AHORA),
            ConexionMeta(status="connected", health=NUMERO_OK, conectada_en=antes),
        ],
        waweb=[],
        manual_en=None,
    )
    assert (paso.estado, paso.completado_en, paso.seccion) == ("completo", antes, "connections")


@pytest.mark.parametrize(
    ("status", "health", "motivo"),
    [
        ("connected", {}, "sin_verificar"),  # conectada, pero el diagnóstico no vio el número
        ("connected", {"number_registered": {"ok": None}}, "sin_verificar"),
        ("degraded", NUMERO_OK, "con_problemas"),
        ("error", {}, "con_problemas"),
        ("pending", {}, "sin_terminar"),
        ("draft", {}, "sin_terminar"),
    ],
)
def test_whatsapp_a_medias_queda_en_curso_con_su_motivo(status: str, health: dict, motivo: str) -> None:
    paso = paso_whatsapp([ConexionMeta(status=status, health=health, conectada_en=AHORA)], waweb=[], manual_en=None)
    assert (paso.estado, paso.motivo, paso.completado_en) == ("en_curso", motivo, None)


def test_una_conexion_desactivada_no_cuenta() -> None:
    paso = paso_whatsapp([ConexionMeta(status="revoked", health=NUMERO_OK, conectada_en=AHORA)], waweb=[], manual_en=None)
    assert (paso.estado, paso.motivo) == ("pendiente", "sin_conexion")


def test_whatsapp_por_qr_conectado_cuenta() -> None:
    paso = paso_whatsapp([], waweb=[SesionWaweb(status="connected", conectada_en=AHORA)], manual_en=None)
    assert (paso.estado, paso.completado_en) == ("completo", AHORA)


@pytest.mark.parametrize(
    ("status", "motivo"),
    [("pending_qr", "sin_terminar"), ("connecting", "sin_terminar"), ("logged_out", "con_problemas")],
)
def test_whatsapp_por_qr_sin_vincular_queda_en_curso(status: str, motivo: str) -> None:
    paso = paso_whatsapp([], waweb=[SesionWaweb(status=status, conectada_en=None)], manual_en=None)
    assert (paso.estado, paso.motivo) == ("en_curso", motivo)


def test_una_conexion_manual_de_meta_activa_cuenta() -> None:
    paso = paso_whatsapp([], waweb=[], manual_en=AHORA)
    assert (paso.estado, paso.completado_en) == ("completo", AHORA)


def test_sin_ninguna_conexion_el_whatsapp_queda_pendiente() -> None:
    paso = paso_whatsapp([], waweb=[], manual_en=None)
    assert (paso.estado, paso.motivo) == ("pendiente", "sin_conexion")


# ------------------------------------------------------------------ catálogo o servicios
def _contenido(tipo: str, cantidad: int, primero_en: datetime | None, seccion: str = "pack_commerce") -> ContenidoCargado:
    return ContenidoCargado(tipo=tipo, cantidad=cantidad, primero_en=primero_en, seccion=seccion)  # type: ignore[arg-type]


def test_el_catalogo_se_cumple_con_lo_que_cargo_cualquier_habilidad() -> None:
    antes = AHORA - timedelta(days=1)
    paso = paso_catalogo(
        [
            ("pack_calendar", _contenido("servicios", 0, None, "pack_calendar:tipos")),
            ("pack_commerce", _contenido("catalogo", 12, antes)),
        ]
    )
    assert (paso.estado, paso.cantidad, paso.completado_en) == ("completo", 12, antes)
    assert (paso.pack, paso.tipo) == ("pack_commerce", "catalogo")


def test_con_habilidades_pero_sin_contenido_el_paso_apunta_a_la_primera() -> None:
    paso = paso_catalogo(
        [
            ("pack_commerce", _contenido("catalogo", 0, None)),
            ("pack_calendar", _contenido("servicios", 0, None, "pack_calendar:tipos")),
        ]
    )
    assert (paso.estado, paso.motivo, paso.pack, paso.tipo, paso.seccion) == (
        "pendiente",
        "sin_contenido",
        "pack_commerce",
        "catalogo",
        "pack_commerce",
    )


def test_sin_habilidad_que_aporte_contenido_el_paso_lleva_a_habilidades() -> None:
    paso = paso_catalogo([])
    assert (paso.estado, paso.motivo, paso.seccion, paso.pack) == ("pendiente", "sin_habilidad", "plugins", None)


# ------------------------------------------------------------------ probar el asistente y equipo
def test_probar_el_asistente_se_cumple_con_el_primer_turno() -> None:
    assert paso_probar_asistente(AHORA).estado == "completo"
    paso = paso_probar_asistente(None)
    assert (paso.estado, paso.motivo, paso.seccion) == ("pendiente", "sin_pruebas", "connections")


def test_el_equipo_se_cumple_con_la_primera_invitacion_o_colaborador() -> None:
    antes = AHORA - timedelta(hours=1)
    assert paso_equipo(AHORA, antes).completado_en == antes
    assert paso_equipo(None, AHORA).completado_en == AHORA
    paso = paso_equipo(None, None)
    assert (paso.estado, paso.motivo, paso.seccion) == ("pendiente", "sin_invitaciones", "collaborators")


# ------------------------------------------------------------------ habilidades pendientes de configurar
PAGOS = Habilidad(nombre="Pagos en línea", guia="Configura tus llaves de Wompi (sandbox o producción).")


def test_una_habilidad_pendiente_aparece_como_paso_con_su_explicacion() -> None:
    pasos = pasos_de_habilidades(
        [{"pack": "pack_payments", "paso": "configurar_pack_payments", "detalle": "faltan datos: public_key"}],
        habilitadas=set(),
        habilidades={"pack_payments": PAGOS},
    )
    assert len(pasos) == 1
    paso = pasos[0]
    assert (paso.clave, paso.estado, paso.motivo, paso.pack, paso.seccion) == (
        "configurar_pack_payments",
        "pendiente",
        "faltan_datos",
        "pack_payments",
        "plugins",
    )
    assert (paso.titulo, paso.explicacion) == (PAGOS.nombre, PAGOS.guia)


def test_la_habilidad_configurada_despues_marca_su_paso_solo() -> None:
    (paso,) = pasos_de_habilidades(
        [{"pack": "pack_payments"}], habilitadas={"pack_payments"}, habilidades={"pack_payments": PAGOS}
    )
    assert (paso.estado, paso.motivo, paso.completado_en) == ("completo", None, None)


def test_pendientes_raros_o_de_habilidades_que_no_existen_no_se_muestran() -> None:
    pasos = pasos_de_habilidades(
        [
            {"pack": "pack_payments"},
            {"pack": "pack_payments"},  # repetido
            {"pack": "pack_desinstalado"},
            {"pack": "../../etc"},
            {"pack": None},
            "texto",
        ],
        habilitadas=set(),
        habilidades={"pack_payments": PAGOS},
    )
    assert [p.clave for p in pasos] == ["configurar_pack_payments"]


# ------------------------------------------------------------------ tipo de negocio que espera un plan
COMERCIO = RequisitoDePreset(nombre="Comercio", plan_minimo="Basic", alcanza=False)


def test_el_tipo_de_negocio_pendiente_por_el_plan_aparece_como_paso() -> None:
    paso = paso_activar_preset(
        {"id": "comercio", "estado": "pendiente", "detalle": "necesita un plan más alto"},
        preset_aplicado_id=None,
        preset_aplicado_en=None,
        requisito=COMERCIO,
    )
    assert paso is not None
    assert (paso.clave, paso.estado, paso.motivo, paso.titulo, paso.plan_requerido, paso.seccion) == (
        "activar_preset",
        "pendiente",
        "plan_insuficiente",
        "Comercio",
        "Basic",
        "plan",
    )


def test_el_tipo_de_negocio_aplicado_despues_queda_completo() -> None:
    paso = paso_activar_preset(
        {"id": "comercio", "estado": "pendiente"},
        preset_aplicado_id="comercio",
        preset_aplicado_en=AHORA,
        requisito=None,
    )
    assert paso is not None and (paso.estado, paso.completado_en) == ("completo", AHORA)


@pytest.mark.parametrize(
    "alta_preset",
    [None, {"id": "comercio", "estado": "aplicado"}, {"id": None, "estado": "sin_vertical"}, {"estado": "pendiente"}],
)
def test_sin_tipo_de_negocio_pendiente_no_hay_paso(alta_preset: dict | None) -> None:
    assert paso_activar_preset(alta_preset, preset_aplicado_id=None, preset_aplicado_en=None, requisito=COMERCIO) is None


def test_con_el_plan_suficiente_el_paso_no_promete_nada() -> None:
    """Si el plan ya alcanza pero el preset no se aplicó, lo aplica HU-SH-04 al subir de plan:
    el panel no ofrece una acción que no existe."""
    alcanza = RequisitoDePreset(nombre="Comercio", plan_minimo="Basic", alcanza=True)
    assert (
        paso_activar_preset(
            {"id": "comercio", "estado": "pendiente"}, preset_aplicado_id=None, preset_aplicado_en=None, requisito=alcanza
        )
        is None
    )


def test_con_las_habilidades_del_preset_ya_habilitadas_no_hay_nada_que_esperar_del_plan() -> None:
    """Elegir la plantilla del asistente habilita sus habilidades: un Free con su agente funcionando no
    debe ver «tu tipo de negocio necesita Basic»."""
    alta = {"id": "comercio", "estado": "pendiente"}
    kwargs = {"preset_aplicado_id": None, "preset_aplicado_en": None, "requisito": COMERCIO}
    assert paso_activar_preset(alta, packs_ya_habilitados=False, **kwargs) is not None
    assert paso_activar_preset(alta, packs_ya_habilitados=True, **kwargs) is None


# ------------------------------------------------------------------ medición
def test_segundos_desde_el_alta_nunca_son_negativos() -> None:
    assert segundos_desde_alta(AHORA, AHORA + timedelta(minutes=90)) == 5400
    assert segundos_desde_alta(AHORA, AHORA - timedelta(minutes=5)) == 0


def test_contar_pasos_completos() -> None:
    pasos = [paso_probar_asistente(AHORA), paso_equipo(None, None), paso_datos_negocio("Tienda", AHORA)]
    assert contar(pasos) == (2, 3)
