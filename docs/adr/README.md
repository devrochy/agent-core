# Decisiones de arquitectura

Un ADR por decisión. Nunca se edita uno aceptado: se escribe otro que lo reemplaza.
Se crean con `/adr`.

> La serie legacy `docs/ADR/` (001-008, modelo single-tenant) está **superseded**;
> esta es la serie vigente.

| # | Decisión | Estado |
|---|---|---|
| 0001 | Multi-tenant pooled con RLS; sin forks por cliente | aceptado |
| 0002 | Gateway propio como punto de ingreso; Chatwoot desacoplado | aceptado |
| 0003 | Contratos congelados; cambios solo por ADR | aceptado |
| 0004 | Tablas por pack con prefijo + `custom` JSONB; prohibido EAV | aceptado |
| 0005 | Tools de primera parte in-process; terceros vía MCP | aceptado |
| 0006 | Lock + debounce por conversación en Redis | aceptado |
| 0007 | Idempotencia por `external_message_id` | aceptado |
| 0008 | `usage_ledger` append-only como fuente de facturación | aceptado |
| 0009 | Releases de agente versionados; prohibido editar prompts en producción | aceptado |
| 0010 | Producción en VPS, nunca en máquina de desarrollo | aceptado |
| 0011 | Runtime y contratos en Python; `@agentcore/contracts` redefinido en Python | aceptado |
| 0012 | Autenticación y contexto de tenant sin Supabase (RLS vía `set_config`) | aceptado |
| 0013 | Claim de idempotencia revocable para DLQ | aceptado |
| 0014 | Formato de playbook y carga just-in-time (allowlist) | aceptado |
| 0015 | Semántica de presupuesto y corte del turno | aceptado |
| 0016 | Canary en despliegue self-hosted de un solo operador | aceptado |
| 0017 | Copy al cliente es CONFIG; los playbooks son instrucciones | aceptado |
| 0018 | Numeración de pedidos por tenant | aceptado |
| 0019 | Registry v1: manifest, enablement por tenant y consentimiento de scopes | aceptado |
| 0020 | Extracción de pack-commerce desde el plugin heredado mod_crm_engine | aceptado |
| 0021 | Topología de despliegue: panel y gateway+consumer en un solo proceso | aceptado |
| 0022 | Leads como vista computada de Customer, no una tabla que sincronizar | aceptado |