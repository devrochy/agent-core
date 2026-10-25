"""Qué filas de `usage_ledger` son turnos y cuáles cuentan como gasto del negocio.

- `embedding` (ingesta del conocimiento, ADR-0145 §3) y `generacion` (datos de prueba, ADR-0146) son
  gasto pero no turnos.
- `eval` (gate de pass^k de una release candidata, HU-AOS-21, adenda de ADR-0117) no es turno ni
  gasto del negocio: lo paga la plataforma y tiene su propio tope. Toda lectura de gasto del negocio
  (cuota de costo, costos, tablero, consola, data por agente) lo excluye.
- `agent_run` (operación A2A de un agente sobre otro, HU-AOS-18, adenda de ADR-0116) es gasto del
  negocio y actividad del agente destino, pero no es un turno: el turno que la originó ya cuenta.

Dos listas porque son dos preguntas: `NO_SON_TURNOS` para contar turnos y `NO_ES_ACTIVIDAD` para la
actividad y el gasto por agente o por conversación (Mi agente y su data, rankings de costo).
"""

from __future__ import annotations

NO_ES_ACTIVIDAD: tuple[str, ...] = ("embedding", "generacion", "eval")
NO_SON_TURNOS: tuple[str, ...] = (*NO_ES_ACTIVIDAD, "agent_run")
EVAL = "eval"
