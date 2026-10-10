"""Procedimientos (`procedure/1`, ADR-0113): catálogo de acciones, validador, linter y
afirmaciones compiladas (HU-AOS-03). El motor que los ejecuta llega con HU-AOS-05;
hoy nada en runtime lee procedimientos."""

from core.kernel.procedures.afirmaciones import compilar_afirmaciones
from core.kernel.procedures.catalogo import ActionInfo, Catalogo, catalogo_de_acciones
from core.kernel.procedures.linter import MAX_PROCEDURE_BYTES, Issue, lint, revisar, validar

__all__ = [
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
