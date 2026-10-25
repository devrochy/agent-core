"""Qué filas de `usage_ledger` son turnos y cuáles cuentan como gasto del negocio.

- `embedding` (ingesta del conocimiento, ADR-0145 §3) y `generacion` (datos de prueba, ADR-0146) son
  gasto pero no turnos.
- `eval` (gate de pass^k de una release candidata, HU-AOS-21, adenda de ADR-0117) no es turno ni
  gasto del negocio: lo paga la plataforma y tiene su propio tope. Toda lectura de gasto del negocio
  (cuota de costo, costos, tablero, consola, data por agente) lo excluye.
"""

from __future__ import annotations

NO_SON_TURNOS: tuple[str, ...] = ("embedding", "generacion", "eval")
EVAL = "eval"
