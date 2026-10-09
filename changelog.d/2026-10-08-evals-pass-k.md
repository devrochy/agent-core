## Evals de consistencia: pass^k, cliente simulado y aserciones de estado (2026-10-08)

- HU-ARCH-28 (ADR-0119, ahora aceptado). `evals/runner.py` y `evals/journeys.py` aceptan `--k`: cada escenario
  corre k veces y se reportan pass@1, pass^k y los escenarios inestables, con lo que cambió entre los intentos
  que pasaron y los que fallaron (herramientas que faltaron o aparecieron y el motivo). El gate sigue midiendo
  pass@1, así que con k = 1 todo queda como antes; el nightly corre k = 4.
- Paso `simulado` en los journeys: un LLM hace de cliente con objetivo, datos, estilo y semilla (prompt en
  `evals/simulador/cliente.md`) y conversa con el loop real hasta cumplir el objetivo, pedir relevo o llegar al tope
  de turnos. Dos journeys nuevos: comprar el Free y comprar el Basic con aserciones sobre el estado final.
- Migración 0058: `eval_runs` y `eval_results` guardan `k`, `pass_1`, `pass_k`, `inestable`, la traza del
  simulador y cada aserción de estado con esperado y real (`verifier` queda listo para HU-ARCH-29).
