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

## Adenda (2026-10-09, HU-SH-08)

Para un asistente cuyos artefactos viven en `TENANTS_ROOT` (playbooks, flujo, voz de marca, catálogo; ADR-0026 y
ADR-0109), el pipeline de esta decisión se cumple así:

- **Release candidato:** el spec y también los artefactos del negocio en su repo (para SolversHub,
  `solvershub-comercial/tenants/solvershub/`).
- **Gate de evals y tenant de staging:** `evals/journeys.py --tenant <slug>` arma una réplica efímera del negocio
  con los artefactos del checkout y corre sus journeys. En cada PR del repo del negocio corre el subconjunto
  estable con k = 8 y umbral 95 % sobre pass@1 (ADR-0119) contra la versión del motor en producción. Si no pasa,
  el PR no se mergea. El set completo corre cada noche contra `develop`, y la release del motor lo lanza con el
  tag candidato. El workflow vive en el repo del negocio, no en el framework.
- **Rollback:** revert del PR en el repo del negocio y reaplicar los artefactos (runbook del negocio). Activar una
  versión del spec con `agent-core agent rollback --version X` solo si el cambio creó una versión nueva.
- **Fuera del gate:** los cambios hechos desde el panel (modelo, persona o spec). Siguen con el rollback en un clic
  de ADR-0051. ADR-0117 (propuesto) los cubrirá con releases por conexión, cuyo build congela estos mismos
  artefactos.

