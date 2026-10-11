"""Procedimientos (`procedure/1`, ADR-0113): catálogo de acciones, validador, linter y
afirmaciones compiladas (HU-AOS-03). El motor que los ejecuta está en `motor.py` (reglas puras),
`ejecucion.py` (con estado), `registro.py` y `sombra.py` (HU-AOS-05); solo actúa con
`tenants.aos_procedures` (o en sombra con `aos_runtime = 'v3'`)."""

from core.kernel.procedures.afirmaciones import compilar_afirmaciones
from core.kernel.procedures.catalogo import ActionInfo, Catalogo, catalogo_de_acciones
from core.kernel.procedures.linter import (
    MAX_ISSUES,
    MAX_PROCEDURE_BYTES,
    Issue,
    lint,
    revisar,
    validar,
)

__all__ = [
    "MAX_ISSUES",
    "MAX_PROCEDURE_BYTES",
    "ActionInfo",
    "Catalogo",
    "Issue",
    "catalogo_de_acciones",
    "compilar_afirmaciones",
    "lint",
    "revisar",
    "validar",
]
