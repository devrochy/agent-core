"""Verificador de afirmaciones con modelo de evidencia (ADR-0119, HU-ARCH-29)."""

from core.orchestrator.verificador.evidencia import EstadoTareas, Evidencia
from core.orchestrator.verificador.verificador import (
    TOGGLES,
    Modos,
    Veredicto,
    cargar_afirmaciones,
    instruccion_correctiva,
    normalizar_desactivados,
    registrar_veredicto,
    verificar,
    verificar_seguro,
)

__all__ = [
    "TOGGLES",
    "EstadoTareas",
    "Evidencia",
    "Modos",
    "Veredicto",
    "cargar_afirmaciones",
    "instruccion_correctiva",
    "normalizar_desactivados",
    "registrar_veredicto",
    "verificar",
    "verificar_seguro",
]
