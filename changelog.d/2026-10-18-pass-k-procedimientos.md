## pass^k de los procedimientos con el LLM real, a demanda (2026-10-18)

- Workflow `evals-procedimientos.yml` (solo `workflow_dispatch`, nunca en PR ni en push): corre el set de
  journeys `procedimientos` con `k` intentos (4 por defecto) sin el motor (`legacy`, línea base de un agente)
  y/o con el motor (`v3` + `aos_procedures`), con `patron` opcional, y sube los dos reportes como artefacto.
  Comparte la concurrencia con `evals-nightly` y usa `secrets.LLM_API_KEY` (ADR-0112, ADR-0119).
- `evals/journeys.py --runtime` en un set con motor de procedimientos: `legacy` ya no enciende el motor y
  `v3` lo enciende (antes `v3` salía con error y `legacy` igual encendía el motor). Sin `--runtime`, todo como
  antes. La corrida se guarda como `<set>@<runtime>` y el comando de regrabar incluye el runtime.
- Journey nuevo `04-agendar-una-demo` (procedimiento `pack_calendar.agendar_cita`): el set `procedimientos`
  suma `pack_calendar` con agenda interna. `promesa_de_pago` y el journey de enrutamiento quedan pendientes.
