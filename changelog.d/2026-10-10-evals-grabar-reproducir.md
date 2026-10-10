## Evals: grabar y reproducir el LLM, y gate en replay en las PRs (2026-10-10)

- Modo grabar/reproducir del LLM para las evals (`core/llm/cassette.py`, adenda de ADR-0119). `evals/runner.py` y
  `evals/journeys.py` aceptan `--grabacion off|record|replay` (o `LLM_CASSETTE`) y `--grabaciones DIR` (o
  `LLM_CASSETTE_DIR`).
  - `record` llama al LLM real y graba un JSON por escenario o journey. Gasta saldo: solo con OK del dueño.
  - `replay` responde desde la grabación sin red ni `LLM_API_KEY`. Si algo cambió, falla con el diff (código de
    salida 3) y nunca cae al LLM real.
- La clave normaliza la fecha y hora del prompt (ADR-0091), los UUIDs, los timestamps y los ids generados. La
  respuesta grabada se rehidrata con los ids de la corrida actual.
- El modo solo se enciende en development o test. Una corrida en replay no se guarda en `eval_runs`.
- CI (adenda de ADR-0120): nuevo job `evals-replay` en las PRs a `develop`. Corre el gate de evals y journeys en replay
  sin `LLM_API_KEY`, con `scripts/gate_evals.sh`. El push a develop y el nightly siguen con el LLM real.
- Guía y flujo para regrabar en `evals/README.md`.
- `scripts/grabar_gate_local.sh`: graba el gate con el LLM real en un stack efímero idéntico al CI, lo reproduce dos
  veces sin clave y revisa que no queden secretos. Tiene ensayos sin LLM (`--ensayo`, `--ensayo-demo`).
- Seguridad: los tracebacks ya no imprimen secretos. `Settings` oculta en `repr` los campos secretos y las contraseñas
  de las URLs. Los runners de evals usan una consola sin variables locales (`configure_console_logging`), y la CLI
  apaga `pretty_exceptions_show_locals`. El contenedor de secretos del modo grabación no se puede imprimir.
