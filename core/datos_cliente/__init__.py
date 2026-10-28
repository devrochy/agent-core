"""Datos del cliente por módulo (HU-CRM-13, ADR-0179)."""

from core.datos_cliente.evaluar import Evaluacion, conocidos_canonicos, evaluar, niveles_efectivos
from core.datos_cliente.modelo import AccionDatos, CampoPolitica, Nivel
from core.datos_cliente.registro import registro_datos

__all__ = [
    "AccionDatos",
    "CampoPolitica",
    "Evaluacion",
    "Nivel",
    "conocidos_canonicos",
    "evaluar",
    "niveles_efectivos",
    "registro_datos",
]
