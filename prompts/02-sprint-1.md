# Prompt 3 — Sprint 1: núcleo multi-tenant (modo plan)

> Requiere Sprint 0 cerrado y etiquetado en git.

---

Lee `CLAUDE.md`, `docs/adr/`, `docs/ROADMAP.md` y `packages/contracts/src/index.ts`.

Planifica el Sprint 1 según `docs/ROADMAP.md`. Antes de escribir el plan, lanza el subagente
`arquitecto` con tu propuesta y ajústala según lo que responda.

Alcance:

1. **Esquema core**: contact, contact_channel, conversation, message, deal, pipeline, stage,
   task, timeline_event, escalation_ticket, agent, agent_release, tool_call, usage_ledger,
   processed_events, tenant, tenant_user.
   Todas siguiendo `/db-migration` sin excepción.

2. **Autenticación**: `tenant_id` como custom claim en el JWT de Supabase, y
   `set_config('app.tenant_id', ...)` al inicio de cada petición autenticada.

3. **Gateway** (`apps/gateway`):
   - Verificación de firma de los webhooks de Meta.
   - Normalización a `InboundMessage`.
   - Dedupe por `external_message_id` insertando en `processed_events` ANTES de procesar
     (ADR-0007).
   - Lock distribuido en Redis por `conversationKey` + debounce de 3 s (ADR-0006).
   - Encolado en BullMQ con cuota de concurrencia por tenant.
   - DLQ con reintento manual.

4. **Tests** que son el criterio de salida:
   - 5 webhooks idénticos en 3 s → exactamente 1 turno procesado.
   - 3 mensajes distintos del mismo contacto en 2 s → 1 solo turno con los 3 agrupados.
   - Un tenant encolando 1.000 mensajes no impide que otro tenant sea atendido.
   - Una query sin `tenant_id` no devuelve filas de otro tenant (test contra RLS real,
     no mockeado).

Al terminar, corre `auditor-multitenant` y no cierres el sprint con hallazgos de severidad alta.

Muéstrame el plan antes de ejecutar.
