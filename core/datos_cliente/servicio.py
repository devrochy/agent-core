"""Política efectiva de un agente para una acción (HU-CRM-13/HU-CRM-28, ADR-0179).

Junta lo que declara el módulo (defecto + `base` de su configuración) con el ajuste del agente.
El agente sale del `ToolContext` cuando la tool lo pasa; si no, del especialista en foco de la
release del turno. Un fallo al leer el ajuste o la base NUNCA bloquea la acción: se usa el defecto
del módulo y se registra (la política es una ayuda, no una compuerta de seguridad: ADR-0179
decisión 9).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from core.datos_cliente.ajustes import cargar_ajustes
from core.datos_cliente.evaluar import Evaluacion, evaluar, niveles_efectivos
from core.datos_cliente.modelo import AccionDatos, Nivel
from core.datos_cliente.registro import registro_datos
from core.shared.logger import get_logger

log = get_logger(__name__)


def agente_del_turno_id() -> uuid.UUID | None:
    """Id del especialista en foco de la release del turno; None fuera de una release."""
    try:
        from core.shared.release_vigente import agente_del_turno

        agente = agente_del_turno()
        return agente.agent_id if agente is not None else None
    except Exception:
        return None


async def base_de(accion: AccionDatos) -> dict[str, Nivel] | None:
    if accion.base is None:
        return None
    try:
        return await accion.base()
    except Exception:
        log.warning("datos_cliente_base_no_disponible", accion=accion.id, exc_info=True)
        return None


async def ajustes_de(accion_id: str, agent_id: uuid.UUID | None = None) -> dict[str, str]:
    try:
        return (await cargar_ajustes(agent_id if agent_id is not None else agente_del_turno_id())).get(
            accion_id, {}
        )
    except Exception:
        log.warning("datos_cliente_ajustes_no_disponibles", accion=accion_id, exc_info=True)
        return {}


async def niveles_del_agente(accion: AccionDatos, agent_id: uuid.UUID | None = None) -> dict[str, Nivel]:
    return niveles_efectivos(
        accion, await ajustes_de(accion.id, agent_id), await base_de(accion)
    )


# Alias del nombre anterior: los packs y `guardar()` lo usan hasta el siguiente release.
niveles_del_negocio = niveles_del_agente


async def evaluar_accion(
    accion_id: str,
    conocidos: Mapping[str, Any] | None,
    *,
    base: Mapping[str, Nivel] | None = None,
    agent_id: uuid.UUID | None = None,
) -> Evaluacion | None:
    """Evaluación de `accion_id` con los datos `conocidos`; `None` si la acción no está registrada.

    `base` lo pasa quien ya tiene la configuración del módulo en la mano (así no se vuelve a cargar y
    ambas vistas del mismo turno coinciden); sin él se pregunta al módulo. `agent_id` es el agente
    que ejecuta la tool; sin él se usa el especialista en foco."""
    accion = registro_datos.obtener(accion_id)
    if accion is None:
        return None
    return evaluar(
        accion,
        conocidos,
        await ajustes_de(accion_id, agent_id),
        base if base is not None else await base_de(accion),
    )
