# Decisiones de arquitectura

Un ADR por decisión. Nunca se edita uno aceptado: se escribe otro que lo reemplaza.
Se crean con `/adr`.

| # | Decisión | Estado |
|---|---|---|
| 0001 | Multi-tenant pooled con RLS; sin forks por cliente | aceptado |
| 0002 | Gateway propio como punto de ingreso; Chatwoot desacoplado | aceptado |
| 0003 | Contratos congelados; cambios solo por ADR | aceptado |
| 0004 | Tablas por pack con prefijo + `custom` JSONB; prohibido EAV | pendiente |
| 0005 | Tools de primera parte in-process; terceros vía MCP | pendiente |
| 0006 | Lock + debounce por conversación en Redis | pendiente |
| 0007 | Idempotencia por `external_message_id` | pendiente |
| 0008 | `usage_ledger` append-only como fuente de facturación | pendiente |
| 0009 | Releases de agente versionados; prohibido editar prompts en producción | pendiente |
| 0010 | Producción en VPS, nunca en máquina de desarrollo | pendiente |

Los pendientes están redactados en `0004-0010-borradores.md`. Sepáralos en ficheros
propios a medida que los implementes: escribir el ADR es parte de la tarea, no un trámite
posterior.
