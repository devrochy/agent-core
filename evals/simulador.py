"""Cliente simulado de los evals de consistencia (HU-ARCH-28, ADR-0119).

Se movió a `core/pruebas/simulador.py` (ADR-0212 §3): el corredor de las pruebas del dueño lo usa desde el worker y
`core/` no importa de `evals/`. Este módulo lo reexporta para el gate y los journeys.
"""

from core.pruebas.simulador import CUMPLIDO, PLAYBOOK, RINDE, ClienteSimulado, _cargar_plantilla

__all__ = ["CUMPLIDO", "PLAYBOOK", "RINDE", "ClienteSimulado", "_cargar_plantilla"]
