"""Registro de opciones de configuración del negocio (SPEC-37, ADR-0166).

Vista declarativa sobre el almacenamiento existente (columnas de `AppSettings`, `behavior_config`,
`escalation_config` y la persona del spec). No tiene tablas ni migraciones. Solo lo importan la API
y el panel de administración: el runtime de turnos no, porque sus textos son para personas y no
para el modelo.
"""

from core.config_registro.catalogo import Catalogo, catalogo_para, opcion
from core.config_registro.modelo import Dependencia, Opcion, ValorPosible
from core.config_registro.opciones import OPCIONES, POR_CLAVE

__all__ = [
    "OPCIONES",
    "POR_CLAVE",
    "Catalogo",
    "Dependencia",
    "Opcion",
    "ValorPosible",
    "catalogo_para",
    "opcion",
]
