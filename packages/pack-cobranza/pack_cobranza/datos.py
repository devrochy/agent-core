"""Acción de Cobranza que pide datos del cliente (HU-AOS-93, ADR-0179 decisión 1, ADR-0198).

Completa los cuatro especialistas: Ventas, Agenda y Suscripciones ya declaraban la suya. El agente de
Cobranza opera sobre un cliente de cartera que ya existe (lo creó el Coordinador o la importación), así
que los defaults NO bloquean la promesa: el documento se recomienda para verificar a la persona y el resto
no se pide. El dueño los sube o baja por agente en `agents.data_policy`.
"""

from __future__ import annotations

from core.datos_cliente import AccionDatos, CampoPolitica, Nivel, registro_datos

PLUGIN_ID = "pack_cobranza"
ACCION_PROMESA = "cobranza.promesa_de_pago"

registro_datos.registrar(
    AccionDatos(
        id=ACCION_PROMESA,
        modulo="cobranza",
        pack=PLUGIN_ID,
        etiqueta="Registrar una promesa de pago",
        campos=(
            CampoPolitica("id_document", "Documento", Nivel.RECOMENDADO),
            CampoPolitica("phone", "Teléfono", Nivel.NO_PEDIR),
            CampoPolitica("email", "Correo", Nivel.NO_PEDIR),
            CampoPolitica("address", "Dirección", Nivel.NO_PEDIR),
        ),
        tools=("cob_registrar_promesa_pago", "cob_registrar_promesa_saldo"),
    )
)
