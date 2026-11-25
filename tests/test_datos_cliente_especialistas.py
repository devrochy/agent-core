"""Los cuatro especialistas declaran sus operaciones de datos del cliente (HU-AOS-93, ADR-0198)."""

from __future__ import annotations

import pack_calendar.datos  # noqa: F401
import pack_cobranza.datos  # noqa: F401
import pack_commerce.datos  # noqa: F401
import pack_subscriptions.datos  # noqa: F401
import pytest

from core.datos_cliente import Nivel, registro_datos
from core.datos_cliente.ajustes import limpiar

ACCIONES_POR_MODULO = {
    "ventas": {"ventas.pedido_domicilio", "ventas.pedido_retiro"},
    "agenda": {"agenda.agendar_cita"},
    "cobranza": {"cobranza.promesa_de_pago"},
    "suscripciones": {"suscripciones.inscribirse"},
}


@pytest.mark.parametrize("modulo", sorted(ACCIONES_POR_MODULO))
def test_cada_tipo_de_agente_declara_sus_operaciones(modulo: str) -> None:
    ids = {a.id for a in registro_datos.de_packs(None) if a.modulo == modulo}
    assert ACCIONES_POR_MODULO[modulo] <= ids


def test_la_promesa_de_cobranza_no_bloquea_por_defecto() -> None:
    accion = registro_datos.obtener("cobranza.promesa_de_pago")
    assert accion is not None and accion.pack == "pack_cobranza"
    # Cobranza opera sobre un cliente de cartera que ya existe: ningún dato frena la promesa.
    assert all(c.nivel is not Nivel.REQUERIDO for c in accion.campos)
    documento = accion.campo("id_document")
    assert documento is not None and documento.nivel is Nivel.RECOMENDADO


def test_la_promesa_solo_aplica_a_agentes_con_sus_tools() -> None:
    accion = registro_datos.obtener("cobranza.promesa_de_pago")
    assert accion is not None
    assert accion.aplica_a({"cob_registrar_promesa_pago", "cob_consultar_saldo"})
    assert accion.aplica_a({"cob_registrar_promesa_saldo"})
    assert not accion.aplica_a({"create_calendar_event", "crear_pedido"})


def test_el_dueno_puede_exigir_el_documento_en_cobranza_y_se_ignora_lo_ajeno() -> None:
    politica = limpiar(
        {
            "cobranza.promesa_de_pago": {"id_document": "requerido", "no_existe": "requerido"},
            "agenda.agendar_cita": {"phone": "requerido"},
        }
    )
    assert politica["cobranza.promesa_de_pago"] == {"id_document": "requerido"}
    assert politica["agenda.agendar_cita"] == {"phone": "requerido"}
