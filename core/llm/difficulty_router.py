"""Difficulty routing: elige el modo del modelo según la dificultad del turno.

Adaptación del ADR-023 de agent-sale a DeepSeek V4:
- `deepseek-flash` (thinking desactivado) -> default. Soporta function calling.
  Cubre el 95% de los turnos.
- `deepseek-flash` (thinking activado) -> razonamiento profundo. Se usa cuando el
  turno no requiere tools (análisis, explicaciones largas) para no pagar tokens
  de razonamiento en cada respuesta.
- `deepseek-chat`/`deepseek-reasoner` se discontinuaron el 2026-07-24 (P-05).

Heurística asimétrica ("error hacia caro, no hacia barato"):
- Se sube a razonamiento ante señales REALES de dificultad.
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
TURNOS_SIN_RESOLVER_THRESHOLD = 2
DIFFICULTY_SCORE_THRESHOLD = 2


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
    long_message_threshold: int = LONG_MESSAGE_THRESHOLD,
    turnos_sin_resolver_threshold: int = TURNOS_SIN_RESOLVER_THRESHOLD,
    score_threshold: int = DIFFICULTY_SCORE_THRESHOLD,
) -> RoutingDecision:
    """Decide el modelo del turno.

    `requires_tools=True` fuerza el modo default (sin thinking): el razonamiento
    V4 no se usa junto con el agentic loop de tools.

    Los 3 umbrales son parametrizables por tenant (pedido explícito del
    usuario, ROADMAP): `AppSettings.behavior_config` en `loop.py` resuelve
    y clampea los valores del tenant, con estas constantes como fallback
    global si no configuró nada. Los PESOS de cada señal (2/1/2 puntos)
    siguen fijos -- son detalle interno, no lo que se pidió parametrizar.
    """
    if not routing_enabled or requires_tools:
        return RoutingDecision(model=default_model, reason="default", allow_tools=True)

    difficulty_score = 0
    if COMPLEX_SIGNALS.search(message):
        difficulty_score += 2
    if len(message) > long_message_threshold:
        difficulty_score += 1
    if turnos_sin_resolver >= turnos_sin_resolver_threshold:
        difficulty_score += 2

    if difficulty_score >= score_threshold:
        return RoutingDecision(model=reasoning_model, reason="complex_signals", allow_tools=False)
    return RoutingDecision(model=default_model, reason="default", allow_tools=True)
