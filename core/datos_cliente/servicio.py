"""Política efectiva de un negocio para una acción (HU-CRM-13, ADR-0179).

Junta lo que declara el módulo (defecto + `base` de su configuración) con el ajuste del dueño.
Un fallo al leer el ajuste o la base NUNCA bloquea la acción: se usa el defecto del módulo y se
registra (la política es una ayuda, no una compuerta de seguridad: ADR-0179 decisión 9).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.datos_cliente.ajustes import cargar_ajustes
from core.datos_cliente.evaluar import Evaluacion, evaluar, niveles_efectivos
from core.datos_cliente.modelo import AccionDatos, Nivel
from core.datos_cliente.registro import registro_datos
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


async def base_de(accion: AccionDatos) -> dict[str, Nivel] | None:
    if accion.base is None:
        return None
    try:
        return await accion.base()
    except Exception:
        log.warning("datos_cliente_base_no_disponible", accion=accion.id, exc_info=True)
        return None


async def ajustes_de(accion_id: str) -> dict[str, str]:
    try:
        return (await cargar_ajustes(get_current_tenant_id())).get(accion_id, {})
    except Exception:
        log.warning("datos_cliente_ajustes_no_disponibles", accion=accion_id, exc_info=True)
        return {}


async def niveles_del_negocio(accion: AccionDatos) -> dict[str, Nivel]:
    return niveles_efectivos(accion, await ajustes_de(accion.id), await base_de(accion))


async def evaluar_accion(
    accion_id: str,
    conocidos: Mapping[str, Any] | None,
    *,
    base: Mapping[str, Nivel] | None = None,
) -> Evaluacion | None:
    """Evaluación de `accion_id` con los datos `conocidos`; `None` si la acción no está registrada.

    `base` lo pasa quien ya tiene la configuración del módulo en la mano (así no se vuelve a cargar y
    ambas vistas del mismo turno coinciden); sin él se pregunta al módulo."""
    accion = registro_datos.obtener(accion_id)
    if accion is None:
        return None
    return evaluar(accion, conocidos, await ajustes_de(accion_id), base if base is not None else await base_de(accion))
