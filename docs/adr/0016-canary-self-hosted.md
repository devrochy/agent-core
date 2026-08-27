# ADR-0016 — Canary en despliegue self-hosted de un solo operador

- **Estado:** aceptado
- **Fecha:** 2026-08-26
- **Reemplaza:** la semántica de "canary al 5%" de ADR-0009 para este despliegue

## Contexto

ADR-0009 pide publicar releases de agente con canary al 5% y rollback en un paso. En un
despliegue self-hosted de un solo operador (sin split de tráfico) el 5% no es factible:
`load_active_spec` selecciona una sola versión activa por tenant, no hay muestreo de versión
ni balanceador para repartir conversaciones entre dos AgentSpecs.

## Decisión

El canary de ADR-0009 se redefine para este despliegue como un **pipeline de gate + staging +
rollback en un paso**:

1. **Gate de evals**: el release candidato (fixture del spec) debe pasar el golden set antes
   de activarse. Detecta regresiones conocidas.
2. **Tenant staging**: el release se valida en un tenant de staging antes de promoverlo al
   tenant real (el framework es multi-tenant: un tenant staging corre el spec nuevo).
3. **Rollback en un paso**: `agent-core agent rollback` revierte a la versión anterior con un
   solo flip de `is_active` en `agent_specs` (la atribución de turnos a versiones ya existe
   vía `usage_ledger.agent_spec_version`).

Se acepta que las regresiones **desconocidas** (no cubiertas por el golden set) se detectan
post-release con monitoreo de métricas (tasa de escalamiento, costo por conversación,
latencia p95, errores de tool) y se revierte con el rollback de un paso.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Canary 5% real con split de tráfico | Requiere muestreo de versión en `load_active_spec` + balanceador; inviable para un operador en VPS |
| Sin canary | El "rollback 1 paso" sin validación previa es frágil |

## Consecuencias

**Positivas:** release reproducible con gate automático; rollback en un paso real (no SQL a
mano); trazabilidad por versión en `usage_ledger`.

**Negativas / lo que aceptamos pagar:** el gate solo cubre regresiones conocidas; la ventana
de detección post-release depende del monitoreo.

**Qué haría que revisáramos esto:** que el gate no atrape un problema real en 10+ releases —
señal de que el golden set está mal diseñado o desactualizado (condición de revisión de
ADR-0009).

## Impacto en el código

`evals/runner.py` (fixture de spec), golden set (`evals/golden_scenarios/`), CLI
(`cli/commands/agent.py`), GitHub Actions (gate de CI).