## Gate de release por pass^k, parte 3a: veredictos, forzado y gasto de evals (HU-AOS-21)

El gate ya lee veredictos. Todavía no hay corredor que los produzca (eso es la parte 3b), así que sin veredicto la candidata pasa como antes, con `sin_evaluar`.

- **Migración 0105:** `eval_runs.release_id` (FK compuesta a `connection_releases`), `eval_runs.origen` y `eval_results.procedure_id`.
- **`core/releases/veredicto.py`:**
  - calcula el pass^4 por procedimiento: fracción de sus escenarios estables, umbral 0,85 y sin bajar frente a la activa con los mismos escenarios;
  - registra en una sola transacción la corrida, los resultados, el gasto (`usage_ledger` con `kind = 'eval'`, a cargo de la plataforma) y el evento `approved` o `rejected` con su `base_release_id`;
  - expone el tope mensual: `tenants.settings.evals.tope_usd_mensual` o `EVAL_TOPE_USD_MENSUAL`, 10 USD por defecto.
- **El gate de `promover`:** un veredicto `rejected` medido contra la activa de ahora da 409 `gate_rechazado`, y uno `approved` pasa. Un veredicto medido contra otra base no cuenta.
- **Forzado:** solo el master puede saltar el gate, con `forzar` y `motivo_forzado` en `POST .../promote`. El motivo queda en el evento y en `audit_log`.
- **Gasto de evals:** queda fuera de toda lectura de gasto del negocio: cuota de costo, costos, tablero, conversación, consola de conexiones, data por agente y primeros pasos.
- Trae también el ajuste del auditor de la parte 2 que no alcanzó a entrar: `hotfix_yanked` queda reservado y la exención `qa_datos` solo vale para negocios no borrados.
