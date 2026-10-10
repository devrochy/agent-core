## Evals: grabar y reproducir el LLM (2026-10-10)

- Modo grabar/reproducir del LLM para las evals (`core/llm/cassette.py`, adenda de ADR-0119). `evals/runner.py` y
  `evals/journeys.py` aceptan `--grabacion off|record|replay` (o `LLM_CASSETTE`) y `--grabaciones DIR` (o
  `LLM_CASSETTE_DIR`).
  - `record` llama al LLM real y graba un JSON por escenario o journey. Gasta saldo: solo con OK del dueño.
  - `replay` responde desde la grabación sin red ni `LLM_API_KEY`. Si algo cambió, falla con el diff (código de
    salida 3) y nunca cae al LLM real.
- La clave normaliza la fecha y hora del prompt (ADR-0091), los UUIDs, los timestamps y los ids generados. La
  respuesta grabada se rehidrata con los ids de la corrida actual.
- El modo solo se enciende en development o test. Una corrida en replay no se guarda en `eval_runs`.
- Guía y propuesta de CI en `evals/README.md`. El CI no cambia.
