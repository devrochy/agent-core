# Roadmap de agent-core

Este fichero es el tablero. Se actualiza al cerrar cada sprint. Claude lo lee para saber
en qué estamos.

**Sprint activo:** Sprint 4
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

- [x] CULTO como tenant #1 sin código específico en el core (el core no tiene dominio de CULTO)
- [x] Playbooks de CULTO convertidos a `SKILL.md` (ventas, seguimiento) y copy a config editable
- [x] `crm_*` multi-tenant: tenant_id + RLS + numeración de pedidos por tenant (ADR-0018)
- [x] Golden set de 35 conversaciones del dominio CRM (sintético; real anonimizado pendiente si se aporta)
- [x] Gate de regresión en CI (GitHub Actions, bloquea) + `make gate` (subconjunto crítico)
- [x] Canary + rollback: ADR-0016 (gate + staging + rollback 1 paso) + `agent-core agent rollback`

**Salida:** CULTO listo para producción sobre agent-core. Faltan de producción real: CULTO
corriendo con credenciales reales, baseline de métricas y el corte (canal Meta + tenant CULTO
configurado). **El hito se cierra en producción, no en el código.**

---

## Sprint 4 — Packs y segundo tenant · Semanas 8–9

- [x] `pack-commerce` extraído de CULTO (ADR-0020)
- [x] FORMOTO como tenant #2 sobre el mismo pack
- [x] Registry v1: manifest, instalación, migraciones por tenant, consentimiento de scopes (ADR-0019)
- [x] Selector de tenant en el panel (cookie `ac_tenant` validada + dropdown)
- [x] Editor de AgentSpec en el panel (form por secciones + JSON crudo; versiona y activa)
- [x] Formulario de configuración autogenerado desde JSON Schema (FormEngine en core/admin/form_generator)
- [x] Renderizador de vistas por descriptor (ViewRegistry + /admin/packs/{pack}/{view}; leads del CRM portado)
- [~] `ext-moto-fitment` — **descartado por validación de negocio**: ForMotos es una
      tienda de repuestos/accesorios de moto (no un motor de compatibilidad). En el
      sistema real (agent-sale) la "compatibilidad técnica" es un motivo de escalación
      a humano, no una tabla de reglas. Se cubre con configuración: trigger de
      escalación "compatibilidad" + copy. (Ver docs/audit o ADR-0020 para el razonamiento.)

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
| ~~2026-08-26~~ | ~~Tablas `crm_*` sin `tenant_id`/RLS~~ | ~~Sprint 1~~ | ~~alta~~ — **RESUELTA en Sprint 3 (crm_0002)** |
| 2026-08-26 | `PluginManager.module.config` se cachea en singleton sin scope de tenant (punto 8 del auditor) | Sprint 1 (Fase A) | media — resolver en el registry por-tenant (Sprint 4) |
| 2026-08-26 | Tools runtime sin `scopes` ni manifest de dominios (`agentpkg.yaml`) | Auditoría inicial | media — Sprint 5 (MCP + allowlist) |
| 2026-08-26 | `ToolContext` runtime sin campo `db` (scoped); plugins usan `get_session()` | Auditoría cierre Sprint 1 | media — alinear con el contrato |
| 2026-08-26 | Activación de plugins global; las skills no se filtran por `AgentSpec.packs` | Auditoría cierre Sprint 1 | media — Sprint 4 (filter skills por packs) |
| 2026-08-26 | Clave Redis de idempotencia sin `tenant_id` | Auditoría cierre Sprint 1 | baja — incluir tenant_id en la clave |
| 2026-08-26 | `llm_usage` (por llamada) fuente legacy; `usage_ledger` es la fuente | Sprint 2 (ADR-0008) | baja — retirar `llm_usage` |
| 2026-08-26 | Pricing LLM hardcodeado en `openai_compatible.py` (CONFIG) | Sprint 2 | media — mover a configuración admin |
| 2026-08-26 | Queries del pack CRM sin filtro `tenant_id` explícito (confían 100% en RLS) | Auditoría cierre Sprint 3 | media — defensa en profundidad (o `ToolContext.db`). **Los 4 puntos que encontró `auditor-multitenant` el 2026-09-10 (`_leads_list`/`_leads_save` en PR #5, `calificar_lead`/`actualizar_lead`/`category_save` en PR #6 — ver `docs/audit/2026-09-10-sesion.md`) ya están resueltos. Esa corrida solo auditó el diff de la sesión, no todo `pack_commerce` — la fila sigue abierta hasta que se audite el resto del pack.** |
| 2026-08-26 | Middleware usa el primer tenant del admin sin selector | Auditoría cierre Sprint 3 | media — selector de tenant en sesión |
| 2026-08-26 | `turn_gate._tenant_sems` sin eviction; provider de embeddings singleton | Auditoría cierre Sprint 3 | baja — evictar / no cachear por tenant |
| 2026-08-26 | Extracción a `pack-commerce`: rename de 13 tablas + `custom jsonb` (ADR-0004) | Sprint 3 (nota) | alta — Sprint 4 |
| 2026-09-10 | El LLM a veces no llama la tool esperada en el primer turno (evals `venta-consultar-inventario`/`venta-recomendar-producto`) ni respeta el límite de longitud en `escalamiento-monto-alto` | Evals, sesión 2026-09-10 | media — requiere ajuste de prompt/playbook, no es bug de código |
| 2026-09-10 | `evals-nightly.yml`: infraestructura arreglada (faltaba `packages/contracts`, clave Fernet inválida) pero nunca se confirmó una corrida completa del golden set (35 escenarios) | Sesión 2026-09-10 | baja — dispararlo y revisar |
| 2026-09-10 | `pack_commerce_categories` sin `deleted_at` — no sigue el patrón de soft-delete del resto del proyecto (invariante nº 8 de `CLAUDE.md`) | Fix de categorías, sesión 2026-09-10 | media — requiere `/db-migration` |
| 2026-09-10 | `sdd/` (docs autogeneradas de un tenant de prueba obsoleto) bloquea portar 4 sub-agentes de OpenCode (`customizer`, `optimizer`, `plugin-dev`, `migrator`) a `.claude/agents/` | Port de tooling, sesión 2026-09-10 | baja — decidir limpiar/mover/regenerar `sdd/` antes de portarlos |
| 2026-09-10 | `.opencode/` y `.claude/` conviven en paralelo sin que quede definido cuál es la fuente de verdad | Port de tooling, sesión 2026-09-10 | baja — riesgo de que diverjan si se edita uno sin el otro |

Cada vez que un onboarding requiera escribir código, se anota aquí: es un pack incompleto.
