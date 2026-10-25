## HU-AOS-21 parte 3b: corredor del gate de pass^k (2026-10-25)

- Nuevo corredor que mide una release candidata contra la activa y escribe el veredicto (`approved` /
  `rejected`) que lee el gate: `core/releases/corredor.py` (reclamo idempotente, cierre, gasto parcial en
  fallos, cinco fallidas por día) y el subproceso `evals/gate_release.py`, con entorno propio contra la base
  `agentcore_eval`, un negocio efímero por set de escenarios y borrado al terminar.
- `core/releases/snapshot.py`: foto de lo que congeló la release (sin datos del cliente) para montarla en la
  base de eval; `core/releases/escenarios.py`: elige los journeys por procedimiento tocado y su huella.
- CLI: `agent-core db migrate --eval` y `agent-core db evaluar-release` (el LLM real exige `--confirmo-costo`;
  `--grabacion replay` corre sin red). El `entrypoint.sh` migra la base de eval si está configurada; el
  `Dockerfile` copia `evals/`.
- Configuración nueva: `EVAL_DATABASE_URL`, `EVAL_MIGRATIONS_DATABASE_URL`, `EVAL_REDIS_DB`,
  `EVAL_TIMEOUT_SEGUNDOS`. `agentcore_eval` se crea a mano una vez (`CREATE DATABASE agentcore_eval`).
- Las grabaciones normalizan también el slug `gate-eval-*` de los negocios efímeros.
- **El gate no se cierra todavía**: sin veredicto una candidata sigue pasando con `gate = sin_evaluar`.
  Falta el encolado (tarea de Celery, endpoint «Evaluar», 409 `gate_pendiente`). No cambia `docs/openapi.json`.
- ADR-0117: adenda «Parte 3b realizada»; `docs/MULTITENANT.md`: el borrado de los efímeros del gate.
