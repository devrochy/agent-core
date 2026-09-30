# ADR-0051 — Operación y gobierno del agente (rollback, horarios, guardrails declarativos, evals)

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

Tras ADR-0050 el `AgentSpec` v2.0.0 es configurable y el agente ya tiene sus campos
con efecto real. Quedan cuatro brechas de **operación y gobierno** que un cliente
empresarial espera y que hoy no existen o viven solo en la CLI:
(1) no se puede **volver a una versión anterior** del AgentSpec desde el panel (solo
`agent-core agent rollback`); (2) no hay **horarios de atención** ni mensaje fuera de
horario ni autoescalado por turnos; (3) los **guardrails propios del negocio** exigen
escribir Python (el único precedente no-code es `completion_claims_custom` de un pack);
(4) los **evals** no se persisten ni se atan a una versión del spec, así que no hay
historial de calidad.

## Decisión

1. **Rollback/activación de versión en el panel.** Nueva ruta
   `POST /admin/configuracion/spec/activar` (sección `settings`): activa una versión
   concreta del `AgentSpecRecord` del tenant (desactiva la actual e inserta el flip en
   una transacción). La lógica se centraliza en `core/agents/spec.py` para que el CLI
   (`cli/commands/agent.py`) y el panel usen la misma. **Sin canary por cohorte**:
   ADR-0016 ya lo descartó para un despliegue self-hosted (gate + staging + rollback).
2. **Horarios y autoescalado (operativo, sin tocar el contrato).** `AppSettings.
   escalation_config` gana `business_hours` (`{tz, ranges:[{day,from,to}]}`),
   `out_of_hours_message` y `auto_escalate_after_turns`. El runtime evalúa la hora del
   tenant (`zoneinfo`) en `core/orchestrator/escalation.py`: fuera de horario responde
   el mensaje configurado; el autoescalado dispara al superar N turnos sin resolver.
   La forma de `business_hours` reutiliza `BusinessHours`/`BusinessHourRange` del
   contrato **como JSON**, sin re-calentar `policies.escalation` en el `AgentSpec`
   (que ADR-0050 eliminó).
3. **Guardrails declarativos por tenant (DSL seguro).** Las reglas viven en
   `AgentSpec.extensions["guardrails"]` (campo ya reservado en v2.0.0) y las evalúa un
   guardrail del core. Kinds **seguros, sin regex crudo** (ADR-0025):
   `forbidden_phrase` (substring, case-insensitive), `required_tool` (si la respuesta
   afirma una acción, exige que la tool se haya llamado) y `max_length`. La acción es
   `retry` (reintento correctivo) o `escalate`. Esto **unifica** el camino con el
   `_persona_guard` existente (forbiddenPhrases/maxLength del spec).
4. **Evals persistidos y atados a la versión del spec.** Nueva tabla de negocio
   `eval_runs` (con `tenant_id`, RLS, índice por tenant) + `eval_results` por
   escenario, con `spec_version` y `git_ref`. `evals/runner.py` persiste cada corrida
   contra la versión activa del tenant; el panel (tab Avanzado) muestra el historial
   de calidad.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Canary con split de tráfico | ADR-0016 lo descartó: no hay balanceador por porcentaje en self-hosted; el sustituto es gate+staging+rollback. |
| Horarios en el `AgentSpec` (`policies.escalation.businessHours`) | ADR-0050 eliminó `policies.escalation`; horarios/SLA son **operativos** (AppSettings) por el principio "versionable → spec, operativo → AppSettings". |
| Guardrails con regex por tenant | Vector de ReDoS e inyección; el repo ya prohibió regex crudo en `completion_claims` (ADR-0025). El DSL seguro cubre los casos reales. |
| Evals sin persistir (status quo) | Sin historial no hay comparación contra baseline ni trazabilidad por versión. |
| Motor de tools declarativas / canales por entry_points | Van en una tanda propia (3B): complejidad alta y requieren ADR de contrato (`ToolContext`). |

## Consecuencias

**Positivas:** el panel gana las capacidades de operación que solo estaban en CLI; el
negocio define sus guardrails sin código ni despliegue; los evals quedan trazados por
versión; todo sin tocar el contrato congelado (reutiliza `extensions`/`escalation_config`).

**Negativas / lo que aceptamos pagar:** el DSL de guardrails es acotado (no cubre
lógica arbitraria; eso seguirá siendo un pack); la evaluación de horarios suma una
dependencia de `zoneinfo` (stdlib, sin paquete nuevo) y un tz inválido cae a un default;
`eval_runs` es una tabla nueva que mantener y limpiar (retención).

**Qué haría que revisáramos esto:** que el negocio necesite lógica de guardrail que el
DSL no exprese (→ pack o motor más rico), o que la operación pida SLA con cola y
asignación (fuera de alcance: es una tanda propia).

## Impacto en el código

`core/agents/spec.py` (helper activar/rollback), `core/admin/views.py` +
`core/admin/templates/settings.html` (rollback, horarios en Identidad, guardrails en
Habilidades, historial de evals en Avanzado), `core/orchestrator/{escalation,consumer,
loop,guardrails}.py` (horarios, autoescalado, motor declarativo),
`evals/runner.py`, `core/db/models.py` + migración `0036` (`eval_runs`/`eval_results`),
`cli/commands/agent.py` (reusa el helper). Tests de integración por pieza.

## Correcciones post-auditoría (`auditor-multitenant`)

Sin hallazgos críticos/altos; se corrigieron los medios y bajos:

- **Fail-safe de horarios**: `within_business_hours` valida tipos (`isinstance`) y
  descarta elementos malformados; una config corrupta ya nunca lanza (no rompe el
  consumer → DLQ). `ranges` ausente/no-lista = siempre abierto; elementos basura se
  ignoran (si ninguna franja válida matchea, se considera cerrado).
- **PII en `escalations.summary`**: el `reason` del guardrail de pack se guarda
  truncado (solo el identificador), no el texto con datos del cliente.
- **`required_tool` sobre turnos previos**: el `audit_log.output` de una tool ahora
  incluye `_tool_name`, así el histórico también aporta al set de tools llamadas.
- **Padding `HH:MM`**: `_normalize_hhmm` normaliza "8:00" → "08:00"; los rangos que
  cruzan medianoche se documentan como no soportados (partir en dos franjas).
- **Envío fuera de horario**: si el mensaje de cierre no se entrega, se escala
  (mismo criterio que el camino inmediato).
- **Clamp** de `auto_escalate_after_turns` a ≤20 en backend.
