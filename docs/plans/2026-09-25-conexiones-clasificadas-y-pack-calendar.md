# Plan — Conexiones clasificadas, correo saliente y pack-calendar v2

- **Estado:** aprobado (ejecución en curso)
- **Fecha:** 2026-09-25
- **Artefacto de research** de la feature (design-sprint fases 1–2). Los PRs se
  descomponen en issues con dependencias (fases 3–4).

## Objetivo

1. Agregar conexiones de canal de **correo** (registro + envío saliente, sin inbound).
2. **Clasificar** las conexiones por categoría (comunicación, correo, calendario).
3. **Unificar** la conexión de Google Calendar en `/admin/conexiones`.
4. Al habilitar `pack_calendar` para un tenant, abrir un **submenú** con todo lo
   necesario para configurar el calendario y que el asistente agende citas.
5. **Superpoder**: si el pack no está habilitado para el tenant, el asistente no
   puede agendar (ya funciona vía `AgentSpec.packs`; se refuerza con prerequisito
   de conexión).

## Decisiones cerradas

- **Clasificación**: modelo híbrido — catálogo de sistema `connection_types` +
  `category` en `channel_connections`.
- **Calendario**: unificado en Conexiones; los tokens OAuth pasan del
  `plugin_configs` al vault de la conexión (`channel_connections.credentials_encrypted`).
  No se usa `meta_private` (es vault por `kind/subject`; aquí hay un token por conexión).
- **OAuth**: se conserva `/admin/plugins/pack_calendar/oauth/callback` (mover el
  redirect URI obligaría a editar Google Console en cada cliente). Solo la UI nueva
  vive en `/calendar/*`.
- **RBAC** del submenú: owner `write`, colaborador `read` (default actual).
- **Correo (PR6)**: se reevalúa al llegar, con el catálogo y el vault ya listos.
  Hueco semántico registrado: `Message.conversation_id` es NOT NULL y
  `outbound_send` resuelve cliente/credenciales desde la conversación, así que un
  canal de correo "canónico" obliga a crear `Customer`+`Conversation` por
  destinatario.

## ADRs

| ADR | Decisión |
|---|---|
| 0069 | Catálogo de tipos de conexión y generalización de `channel_connections`. |
| 0070 | Vault unificado de conexiones: tokens OAuth de Google al vault. |
| 0071 | Correo saliente (solo si PR6 avanza). |
| 0072 | Gating declarativo de tools por prerequisitos de conexión. |

El re-prefix `/calendar` se apoya en ADR-0061/0063; no necesita ADR propio.

## Hallazgos de investigación

- **Regresión de primacía**: `save_connection` marca primaria la primera conexión
  activa (`core/gateway/connections.py:215-222`) y `primary_connection()`/
  `effective_credentials()` buscan por `is_primary` sin filtrar por tipo
  (`connections.py:64`, `core/gateway/sender.py:156`). Conectar correo/calendario
  antes que WhatsApp rompería el fallback del sender de WhatsApp. Fix: primacía
  acotada a conexiones `produces_conversations=true`.
- **Fuga potencial en `save_connection`**: el lookup por `external_id`
  (`connections.py:188-194`) no filtra tenant. Con emails/cuentas Google repetibles
  entre tenants cruzaría datos. Fix: filtro `(tenant_id, type_id)`.
- **Patrón de catálogo**: `plans` (ADR-0045) es el precedente exacto — fuente tipada
  en código + tabla de sistema sin RLS + `sync_plans()` con `upsert_plan()`
  `SECURITY DEFINER`, llamado desde el lifespan (`core/main.py:52`).
- **Formulario**: `credentials_schema` es JSON Schema y se renderiza con el FormEngine
  existente, que ya soporta secretos (`format:password`/`writeOnly`,
  `core/admin/form_generator.py:75,120`). No hace falta UI nueva.
- **Aislamiento**: tablas nuevas con `tenant_id` + RLS + `force row level security`
  + índice que empieza por `tenant_id` + `deleted_at`.

## PRs

### PR1 — Core: catálogo + generalización (sin cambio visible)
ADR-0069. Migración core `0038`, `core/gateway/connection_types.py`,
`sync_connection_types()`, generalización de `core/gateway/connections.py`, tests de
aislamiento y primacía.

### PR2 — Admin Conexiones desde el catálogo
`conexiones.html` generado desde `connection_types` por categoría; `setup_kind`
(`manual|wizard`); formulario genérico de credenciales (FormEngine + SecretBox).
`MetaConnection` convive enlazada por `type_id`.

### PR3 — Google Calendar al vault
ADR-0070. Callback crea/actualiza la conexión; dual-write/dual-read a
`plugin_configs`; `external_id` = email de la cuenta vía `oauth2/v2/userinfo`;
`expires_at` tipado `int`.

### PR4 — pack-calendar v2
Alembic propio; tablas `pack_calendar_appointment_types` y
`pack_calendar_appointments`; router `/calendar` con Conexión/Configuración/Tipos/
Citas; `admin_menu` con `section`+`section_key`; horarios en `CalendarConfig`.

### PR5 — Gating por prerequisito
ADR-0072. `MCPTool.requires_connection_types`; filtro pre-prompt (`loop.py:234-236`)
y `_tool_denial` (`loop.py:709`). Calendar tools requieren `google_calendar` activo.

### PR6 — Correo saliente (parqueado)
ADR-0071. Se reevalúa con caso de uso real. Modelo canónico: `Message` + adaptador
saliente (`agent_core.channels`, ADR-0052), sin tabla de envío paralela.

## Gates

- Por PR: `ruff check` && `pytest`; `auditor-multitenant` (crítico PR1/PR4);
  `/db-migration` y `/adr`; actualizar `ROADMAP.md`/`CHANGELOG.md`.
- Ningún cambio de contrato congelado: `"email"` ya existe en `ChannelKind`.
