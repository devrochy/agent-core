## Evals: los journeys muestran el borrador que bloquea un guardrail (2026-10-23)

- `evals/journeys.py` anota, por turno, el borrador y el motivo de cada bloqueo de `run_guardrails`
  (línea `borrador bloqueado (...)` en el registro del intento). Solo observa: no cambia el veredicto ni el
  loop, y el texto no se escribe en logs ni en `audit_log` de producción (en un journey es de un cliente
  simulado). Sin esto, un intento que escalaba por `calendar_completion_check` no decía qué frase lo
  bloqueó (pass^4 de los procedimientos, 2026-10-22: `agendar-una-demo` 3/4).
