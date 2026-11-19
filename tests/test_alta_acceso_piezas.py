"""Piezas puras del acceso del dueño en el alta desde el panel (ADR-0175)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from core.alta_acceso import InvitacionOwner, _nombre_para_correo, asunto_y_cuerpo, correo_valido


def _inv(*, con_enlace: bool) -> InvitacionOwner:
    return InvitacionOwner(
        invitation_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        email="dueno@example.test",
        con_enlace=con_enlace,
        enlace="https://panel.example.test/invitacion#tok" if con_enlace else "https://panel.example.test",
        vence=datetime(2026, 11, 3, tzinfo=UTC),
    )


def test_el_correo_con_enlace_dice_la_vigencia_y_no_lleva_el_negocio_en_el_asunto() -> None:
    asunto, cuerpo = asunto_y_cuerpo(_inv(con_enlace=True), negocio="Pizzería Ana", nombre=None, origen="panel")
    assert asunto == "Tu acceso a SolversHub"
    assert "Pizzería Ana" not in asunto
    assert "«Pizzería Ana»" in cuerpo and "/invitacion#tok" in cuerpo and "03/11/2026" in cuerpo


def test_la_invitacion_en_la_app_del_panel_no_habla_de_una_compra() -> None:
    asunto, cuerpo = asunto_y_cuerpo(_inv(con_enlace=False), negocio="Pizzería Ana", nombre=None, origen="panel")
    assert asunto == "Te dieron acceso a un negocio en SolversHub"
    assert "compra" not in cuerpo and "#" not in cuerpo
    # El alta automática conserva su texto.
    _asunto, compra = asunto_y_cuerpo(_inv(con_enlace=False), negocio="Pizzería Ana", nombre=None)
    assert "Si no reconoces esta compra" in compra


def test_el_nombre_del_negocio_en_el_correo_no_lleva_enlaces_ni_saltos_de_linea() -> None:
    assert _nombre_para_correo("Mi  negocio\nbonito") == "Mi negocio bonito"
    assert _nombre_para_correo("Visita https://malo.example") == "tu negocio"
    assert _nombre_para_correo("promo.malo.co") == "tu negocio"
    assert _nombre_para_correo("   ") == "tu negocio"
    assert len(_nombre_para_correo("a" * 200)) == 80


def test_correo_valido_normaliza_y_rechaza() -> None:
    assert correo_valido("  Dueno@Example.TEST ") == "dueno@example.test"
    assert correo_valido("sin-arroba") is None
    assert correo_valido("a@b") is None
    assert correo_valido(None) is None
    assert correo_valido("a" * 250 + "@x.co") is None
