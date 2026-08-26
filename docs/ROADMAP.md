# Roadmap de agent-core

Este fichero es el tablero. Se actualiza al cerrar cada sprint. Claude lo lee para saber
en qué estamos.

**Sprint activo:** Sprint 3
**Última actualización:** 2026-08-26

---

## Sprint 0 — Cimientos · Semana 1

**Objetivo:** que exista un framework que defender, aunque todavía no haga nada.

- [x] `docs/inventory.md` completo y revisado a mano *(completo; revisión manual de Rob pendiente)*
- [x] Monorepo Python (uv/pyproject) con la estructura de `CLAUDE.md`
- [x] `@agentcore/contracts` Python v1.0.0 congelado (ADR-0011) y compilando
- [x] ADR-0004 a 0010 separados en ficheros propios
- [x] Devkit verificado: el hook de RLS bloquea una migración de prueba sin RLS
- [x] `auditor-multitenant` corrido contra el código actual, hallazgos en `docs/audit/`

**Salida:** los contratos compilan y el hook bloquea de verdad (pruébalo a propósito).

---

## Sprint 1 — Núcleo multi-tenant · Semanas 2–3

- [x] Esquema core con `tenant_id` + RLS en todas las tablas
- [x] `tenant_id` como claim del contexto autenticado (auth propia, ADR-0012)
- [x] Gateway: verificación de firma de Meta
- [x] Dedupe por `external_message_id` (ADR-0007)
- [x] Lock + debounce por conversación en Redis (ADR-0006)
- [x] Colas con cuota de concurrencia por tenant
- [x] DLQ con reintento manual desde el panel

**Salida:** test que envía 5 webhooks duplicados en 3 s y produce exactamente 1 respuesta. ✅
`make test-integration`: 4 tests de salida en verde contra el stack real.

---

## Sprint 2 — Runtime · Semanas 4–5

- [x] `AgentSpec` leído desde base de datos (cero prompts en código, ADR-0014)
- [x] Tool loop con límite de llamadas y presupuesto por conversación (ADR-0015)
- [x] 8 tools nativas del core
- [x] Tool `load_playbook` + índice de playbooks (allowlist)
- [x] `usage_ledger` append-only (ADR-0008): una fila por turno con `agent_spec_version`
- [x] Trazas OpenTelemetry con costo por conversación

**Salida:** el panel muestra costo por conversación y por tenant, en vivo. ✅
`/admin/costos` lee `usage_ledger` por tenant + por conversación; `make test-integration` verde (95 tests).

---

## Sprint 3 — CULTO migrado · Semanas 6–7

- [ ] CULTO como tenant #1 sin código específico en el core
- [ ] Playbooks de CULTO convertidos a `SKILL.md`
- [ ] Golden set de 30–50 conversaciones reales anonimizadas
- [ ] Gate de regresión en CI
- [ ] Canary + rollback funcionando

**Salida:** CULTO en producción sobre agent-core con métricas iguales o mejores que hoy.
**Este es el hito que valida todo el proyecto. No se salta.**

---

## Sprint 4 — Packs y segundo tenant · Semanas 8–9

- [ ] `pack-commerce` extraído de CULTO
- [ ] `ext-moto-fitment` (compatibilidad marca/línea/cilindraje/año)
- [ ] FORMOTO como tenant #2 sobre el mismo pack
- [ ] Registry v1: manifest, instalación, migraciones por tenant, consentimiento de scopes
- [ ] Formulario de configuración autogenerado desde JSON Schema
- [ ] Renderizador de vistas por descriptor

**Salida:** dar de alta un tercer cliente de comercio toma menos de un día y es 100%
configuración. **Cronometrarlo.** Ese número es la métrica de negocio principal.

---

## Sprint 5 — Extensibilidad y confianza · Semanas 10–11

- [ ] Registro de servidores MCP por tenant con allowlist y circuit breaker
- [ ] Guardrails de runtime: pre-tool, post-generación, presupuesto, rate limit
- [ ] Reporte mensual automático por cliente
- [ ] `docs/security.md` completo

**Salida:** conectar el calendario o el ERP de un cliente sin escribir código nuevo.

---

## Sprint 6 — Paquete comercial · Semana 12

- [ ] SLO publicados y medidos
- [ ] Dashboard de calidad visible para el cliente
- [ ] Contrato + SLA + tratamiento de datos revisados por abogado
- [ ] Backup restaurado una vez, con evidencia
- [ ] Respuestas escritas a las 10 preguntas de venta
- [ ] Onboarding cronometrado por debajo de lo prometido

**Salida:** poder entrar a una reunión con un director de operaciones y responder con
documentos, no con promesas.

---

## Deuda registrada

| Fecha | Deuda | Origen | Prioridad |
|---|---|---|---|
| 2026-08-26 | Tablas `crm_*` del plugin sin `tenant_id`/RLS (13 tablas) | Excepción documentada en Sprint 1 (Fase A) | **alta** — migrar en Sprint 3 ANTES de CULTO en producción |
| 2026-08-26 | `PluginManager.module.config` se cachea en singleton sin scope de tenant (punto 8 del auditor) | Sprint 1 (Fase A) | media — resolver en el registry por-tenant (Sprint 4) |
| 2026-08-26 | Tools runtime sin `scopes` ni manifest de dominios (`agentpkg.yaml`) | Auditoría inicial | media — Sprint 5 (MCP + allowlist) |
| 2026-08-26 | `ToolContext` runtime sin campo `db` (scoped); plugins usan `get_session()` | Auditoría cierre Sprint 1 | media — Sprint 2 (alinear con el contrato) |
| 2026-08-26 | Crons de plugins corren sin `tenant_scope` | Auditoría cierre Sprint 1 | media — iterar tenants al ejecutar crons |
| 2026-08-26 | Activación de plugins global (singleton) vs enablement por tenant | Auditoría cierre Sprint 1 | media — Sprint 2/4 (AgentSpec.packs + registry) |
| 2026-08-26 | Clave Redis de idempotencia sin `tenant_id` (fallback del claim por-tenant) | Auditoría cierre Sprint 1 | baja — incluir tenant_id en la clave |
| 2026-08-26 | `llm_usage` (por llamada) queda como fuente legacy; `usage_ledger` es la fuente de costo | Sprint 2 (ADR-0008) | baja — retirar `llm_usage` en Sprint 3 |
| 2026-08-26 | Pricing LLM hardcodeado en `openai_compatible.py` (CONFIG) | Sprint 2 | media — mover a configuración admin |

Cada vez que un onboarding requiera escribir código, se anota aquí: es un pack incompleto.
