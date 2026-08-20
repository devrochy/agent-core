"""Difficulty routing: elige el modelo según la dificultad del turno.

Adaptación del ADR-023 de agent-sale para DeepSeek:
- deepseek-chat     -> default. Soporta function calling. Cubre el 95% de los turnos.
- deepseek-reasoner -> razonamiento profundo. NO soporta function calling, por lo
  que SOLO se usa cuando el turno no requiere tools (análisis, explicaciones largas).

Heurística asimétrica ("error hacia caro, no hacia barato"):
- Se sube a reasoner ante señales REALES de dificultad.
- Nunca se degrada por debajo del default.
- Sin llamada extra a un LLM clasificador (costo cero de routing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COMPLEX_SIGNALS = re.compile(
    r"\b(por qué|porqué|compara|comparar|diferencia|análisis|analiza|explica en detalle|"
    r"paso a paso|estrategia|calcula|proyección|ventajas y desventajas|pros y contras)\b",
    re.IGNORECASE,
)

LONG_MESSAGE_THRESHOLD = 400  # caracteres


@dataclass
class RoutingDecision:
    model: str
    reason: str
    allow_tools: bool


def route_model(
    message: str,
    turnos_sin_resolver: int,
    default_model: str,
    reasoning_model: str,
    routing_enabled: bool,
    requires_tools: bool,
) -> RoutingDecision:
    """Decide el modelo del turno.

    `requires_tools=True` fuerza el modelo default: deepseek-reasoner no soporta
    function calling y el agentic loop necesita tools disponibles.
    """
    if not routing_enabled or requires_tools:
        return RoutingDecision(model=default_model, reason="default", allow_tools=True)

    difficulty_score = 0
    if COMPLEX_SIGNALS.search(message):
        difficulty_score += 2
    if len(message) > LONG_MESSAGE_THRESHOLD:
        difficulty_score += 1
    if turnos_sin_resolver >= 2:
        difficulty_score += 2

    if difficulty_score >= 2:
        return RoutingDecision(model=reasoning_model, reason="complex_signals", allow_tools=False)
    return RoutingDecision(model=default_model, reason="default", allow_tools=True)
