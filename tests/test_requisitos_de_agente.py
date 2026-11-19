"""Requisitos de una plantilla para un negocio (HU-AOS-50, ADR-0155): función pura, sin base de datos."""

from __future__ import annotations

from datetime import UTC, datetime

from core.agents import requisitos
from core.agents.requisitos import InfoDeHabilidad
from core.presets import load_plantilla
from core.primeros_pasos.contrato import ContenidoCargado

INFO = {
    "pack_commerce": InfoDeHabilidad("Ventas"),
    "pack_payments": InfoDeHabilidad("Cobro en línea", "Pega tus llaves de Wompi."),
    "pack_calendar": InfoDeHabilidad("Agenda"),
    "pack_cobranza": InfoDeHabilidad("Cobranza"),
    "pack_subscriptions": InfoDeHabilidad("Suscripciones", requiere=("pack_payments",)),
}


def _contenido(tipo: str, cantidad: int, seccion: str = "pack_commerce:productos") -> ContenidoCargado:
    return ContenidoCargado(tipo=tipo, cantidad=cantidad, primero_en=datetime.now(UTC) if cantidad else None, seccion=seccion)  # type: ignore[arg-type]


def _claves(rs: list[requisitos.Requisito]) -> list[tuple[str, str]]:
    return [(r.pack, r.tipo) for r in rs]


def test_ventas_sin_cobro_ni_catalogo() -> None:
    pl = load_plantilla("ventas")
    rs = requisitos.calcular(pl, {"pack_commerce"}, {"pack_commerce": _contenido("catalogo", 0)}, INFO)
    assert _claves(rs) == [("pack_payments", "habilidad_sin_configurar"), ("pack_commerce", "sin_contenido")]
    cobro = rs[0]
    assert (cobro.titulo, cobro.explicacion, cobro.seccion) == ("Cobro en línea", "Pega tus llaves de Wompi.", "plugins")
    assert (rs[1].contenido, rs[1].seccion) == ("catalogo", "pack_commerce:productos")


def test_ventas_completo_no_avisa_nada() -> None:
    pl = load_plantilla("ventas")
    rs = requisitos.calcular(
        pl, {"pack_commerce", "pack_payments"}, {"pack_commerce": _contenido("catalogo", 12)}, INFO
    )
    assert rs == []


def test_suscripciones_pide_cobro_en_linea_por_la_habilidad_que_lo_requiere() -> None:
    pl = load_plantilla("suscripciones")
    rs = requisitos.calcular(pl, {"pack_subscriptions"}, {}, INFO)
    assert _claves(rs) == [("pack_payments", "habilidad_sin_configurar")]
    assert requisitos.calcular(pl, {"pack_subscriptions", "pack_payments"}, {}, INFO) == []


def test_agenda_no_avisa_del_catalogo_de_comercio_acotado_a_crear_lead() -> None:
    """Agenda toma de Ventas solo `crear_lead`: que el catálogo esté vacío no le afecta."""
    pl = load_plantilla("agenda")
    contenidos = {"pack_calendar": _contenido("servicios", 0, "pack_calendar:tipos"), "pack_commerce": _contenido("catalogo", 0)}
    rs = requisitos.calcular(pl, {"pack_calendar", "pack_commerce"}, contenidos, INFO)
    assert _claves(rs) == [("pack_calendar", "sin_contenido")]
    assert rs[0].contenido == "servicios"


def test_cobranza_sin_cartera() -> None:
    pl = load_plantilla("cobranza")
    rs = requisitos.calcular(pl, {"pack_cobranza"}, {"pack_cobranza": _contenido("cartera", 0, "pack_cobranza:clientes")}, INFO)
    assert _claves(rs) == [("pack_cobranza", "sin_contenido")]


def test_habilidad_obligatoria_faltante_no_se_repite_aqui() -> None:
    """Cobranza sin la habilidad ya sale como `missing_skills` de la plantilla, no como requisito."""
    assert requisitos.calcular(load_plantilla("cobranza"), set(), {}, INFO) == []


def test_un_pack_que_no_pudo_leerse_no_tumba_ni_avisa() -> None:
    pl = load_plantilla("ventas")
    assert _claves(requisitos.calcular(pl, {"pack_commerce", "pack_payments"}, {}, INFO)) == []


def test_un_tipo_de_contenido_desconocido_no_rompe_la_respuesta() -> None:
    from core.api.routers.agent_editor import _requirement

    r = requisitos.Requisito("pack_x", "sin_contenido", "X", "", "plugins", contenido="otro_tipo")
    assert _requirement(r).content is None
    assert _requirement(requisitos.Requisito("pack_x", "sin_contenido", "X", "", "plugins", contenido="cartera")).content == "cartera"
