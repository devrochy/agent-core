"""Rangos de las opciones numéricas de configuración (SPEC-37, HU-CFG-03).

Módulo hoja: sin textos de interfaz y sin importar nada del runtime. Lo leen el registro de
opciones (`core/config_registro`), los modelos de entrada de la API, el panel SSR y el runtime
de turnos (`loop.py`, `waweb/policy.py`). Así el rango de una opción se escribe en un solo sitio
y el runtime no depende del registro, cuyos textos no pueden llegar a un prompt (invariante 3).
"""

from __future__ import annotations

from typing import NamedTuple


class Rango(NamedTuple):
    minimo: int
    maximo: int

    def recortar(self, valor: int) -> int:
        return max(self.minimo, min(self.maximo, valor))


RANGOS: dict[str, Rango] = {
    # Calidad
    "guardrail_max_retries": Rango(0, 5),
    "difficulty_long_message_threshold": Rango(50, 5000),
    "difficulty_turnos_sin_resolver_threshold": Rango(1, 20),
    "difficulty_score_threshold": Rango(1, 10),
    # Escalamiento
    # ADR-0051: el runtime siempre recortó a 20; la API anunciaba 1000 y aceptaba valores que se ignoraban.
    "auto_escalate_after_turns": Rango(0, 20),
    # Voz
    "persona_warmth": Rango(0, 5),
    "persona_max_length": Rango(0, 10_000),
    # WhatsApp Web (ADR-0074)
    "waweb_send_min_delay_ms": Rango(0, 60_000),
    "waweb_send_max_delay_ms": Rango(0, 120_000),
    "waweb_daily_outbound_cap": Rango(0, 10_000),
}


def rango(clave: str) -> Rango:
    """El rango de una opción numérica (KeyError si la clave no es numérica)."""
    return RANGOS[clave]


def leer_entero(config: dict | None, clave: str, defecto: int) -> int:
    """Lee `clave` de un JSONB de configuración y la recorta a su rango.

    Tolerante a propósito: un valor ausente, `None`, no numérico o fuera de rango (una fila escrita
    por un script o anterior a esta validación) nunca tumba un turno; se usa el defecto o el
    extremo más cercano.
    """
    crudo = (config or {}).get(clave)
    try:
        valor = int(crudo if crudo is not None else defecto)
    except (TypeError, ValueError):
        valor = defecto
    return RANGOS[clave].recortar(valor)
