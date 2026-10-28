"""Acción de Agenda que pide datos del cliente (HU-CRM-13, ADR-0179 decisión 1).

El correo es un piso técnico (`ajustable=False`): sin correo no hay confirmación de la cita
(corrección de la prueba del 2026-10-05), así que el negocio no lo puede bajar.
"""

from __future__ import annotations

from typing import Any

from core.datos_cliente import AccionDatos, CampoPolitica, Nivel, registro_datos
from core.datos_cliente.servicio import evaluar_accion
from core.shared.logger import get_logger

log = get_logger(__name__)
PLUGIN_ID = "pack_calendar"
ACCION_AGENDAR = "agenda.agendar_cita"

registro_datos.registrar(
    AccionDatos(
        id=ACCION_AGENDAR,
        modulo="agenda",
        pack=PLUGIN_ID,
        etiqueta="Agendar una cita",
        campos=(
            CampoPolitica("email", "Correo", Nivel.REQUERIDO, ajustable=False),
            CampoPolitica("name", "Nombre completo", Nivel.RECOMENDADO),
            CampoPolitica("phone", "Teléfono", Nivel.NO_PEDIR),
        ),
    )
)


async def sobre_faltan_datos(conocidos: dict[str, Any]) -> dict[str, Any] | None:
    """Sobre unificado `faltan_datos` para el `needs_data` de agendar. Nunca rompe la tool."""
    try:
        evaluacion = await evaluar_accion(ACCION_AGENDAR, conocidos)
    except Exception:
        log.warning("agenda_sobre_faltan_datos_fallo", exc_info=True)
        return None
    return evaluacion.como_sobre() if evaluacion is not None else None
