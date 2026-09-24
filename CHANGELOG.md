# Changelog

## Sin versión — pack de cobranza (2026-09-24)

Nuevo vertical **`pack-cobranza`**: carteras, préstamos y negociación de cobranza
por WhatsApp. Ver ADR-0056/0057/0058/0059/0060.

> **Alineado a `app-cobranza` develop** (ADR-0060): vocabulario **Propietario →
> Cartera → Cliente → Préstamo** (no socios/rutas), con mapeo de compatibilidad
> 1:1 documentado. 20 tablas en v1.

### Pack (`packages/pack-cobranza/`)
- **21 tablas** `pack_cobranza_*` con `tenant_id` + RLS + FORCE RLS; PostGIS para
  la geografía de clientes (extensión + `geography(Point,4326)` + trigger + GIST).
- **Bloque A (operación)**: crear préstamo (genera cuotas), registrar pago y abono
  (mueven caja y saldo), detalle de cliente con cuotas/abonos, y **auditoría
  imborrable** (`pack_cobranza_auditoria`) de las operaciones. Tablas de cambios de
  cliente y evidencias de cliente.
- **Bloque B (operación de cartera)**: pestañas **Notas** (CRUD), **Estadísticas**
  (KPIs por cartera) y **Trayecto diario** con mapa (Leaflet/OSM), apertura del día
  y registro de posiciones. Tablas `cartera_notas`, `carteras_aperturas`,
  `posicion_gestor`, `reportes_diarios`, `cartera_estadisticas`, `cartera_optimizada`.
- **Bloque C (monitoreo)**: sección **Monitoreo** con **Reglas de negociación**
  (edita la fila por tenant que usa el motor determinista) y **Monitoreo IA**
  (conversaciones/escalaciones del core + promesas del asistente).
- **5 tools del asistente** (orientadas al cliente): consultar saldo, estado de
  cuenta, **evaluación determinista de negociación**, registrar promesa y abono.
  Las operaciones de administración (gasto, inyección, visita, liquidación) viven
  en el panel, no en el asistente del deudor (auditoría M3). Guardrail
  `cob_promesa_check` y cron `marcar_mora`.
- **Panel propio (`admin_router`, `/cobranza/…`)**: Resumen con KPIs, CRUD de
  Gestores/Carteras/Clientes, matriz de **permisos del gestor** (14) y **detalle de
  cartera con pestañas** (Resumen · Configuración · Clientes · Gastos · Inyecciones ·
  Liquidaciones). `cartera-config` con los 32 flags.
- **El propietario ES el tenant** (ADR-0062): sin entidad `propietarios` ni
  `propietario_id`; su config vive en `CobranzaConfig` y sus permisos en el RBAC
  del core (owner=write, colaborador=read).
- `flow.json` con las secciones de atención.

### Core
- **ADR-0056**: leads globales con `LeadProvider` (`core/admin/lead_registry.py`),
  sección `leads` y vista `/admin/leads` que combina `Customer` + columnas por pack.
- **ADR-0058**: imagen de Postgres con **PostGIS + pgvector** (`docker/postgres/Dockerfile`).
- **ADR-0057/0059**: diseño de rutas públicas de pack e identidad de pack, y
  aislamiento multi-tenant del pack (1 tenant = 1 propietario).
- **ADR-0060**: alineación del pack al modelo de dominio de `app-cobranza`
  develop (mapeo de compatibilidad Propietario/Gestor/Cartera).
- **ADR-0061**: `admin_menu` de packs por **secciones** (`section`) y visibilidad
  por rol (`master_only`); el menú del pack replica la IA del panel de referencia.
- **ADR-0062**: el propietario es el tenant; el pack no duplica identidad.
- Menú de packs **desplegable** en el panel (`core/admin/templates/base.html`).

## v0.7.0 — Operación y gobierno del agente (2026-09-23)

Capacidades empresariales de operación sobre lo construido en ADR-0049/0050. Ver ADR-0051.

### Rollback de AgentSpec en el panel
- Botón "Activar" por versión en el tab Avanzado; helper único
  `activate_spec_version` (CLI y panel). Sin canary (ADR-0016).

### Horarios y autoescalado
- `AppSettings.escalation_config` gana `business_hours` (tz + franjas), mensaje fuera
  de horario y `auto_escalate_after_turns`. Evaluación con `zoneinfo`, fail-safe ante
  config corrupta. UI en el tab Identidad.

### Guardrails declarativos por tenant
- Motor de reglas (DSL seguro, sin regex crudo) en `AgentSpec.extensions["guardrails"]`:
  `forbidden_phrase`, `required_tool`, `max_length`, con acción `retry`/`escalate`.
  UI en el tab Habilidades.

### Evals por versión de spec
- Tablas `eval_runs` / `eval_results` (migración 0036, RLS); el runner persiste cada
  corrida atada a `spec_version` y el tab Avanzado muestra el historial de calidad.

## v0.6.0 — Configuración por pestañas, pagos BYOK y AgentSpec v2 (2026-09-23)

Panel configurable por secciones, cobro BYOK del tenant y un contrato de agente
que deja de prometer en vano. Ver ADR-0047/0048/0049/0050.

### RBAC y colaboradores (ADR-0047/0048)
- Permisos de sección configurables (`admin_permissions`, global o por tenant).
- Admin por tenant (`TenantUser.owner`) con login propio; página "Colaboradores".
- CLI `agent-core admin` y alta de tenant con credenciales del administrador.

### Configuración por pestañas y pagos BYOK (ADR-0049)
- `/admin/configuracion` en pestañas: **Identidad** y **Pagos** (esta última si el
  pack `pack-payments` está habilitado).
- Pantalla de pagos: credenciales Wompi por tenant cifradas, "Probar y guardar"
  (link real por el mínimo), URL del webhook.
- `BaseModule.extra_admin_routers()`: packs aportan pestañas admin sin tocar el core.

### AgentSpec v2.0.0 (ADR-0050)
- El contrato pasa a v2.0.0: campos antes muertos pasan a tener efecto real o se
  eliminan. `temperature`, `model.fallback`/`router`, `memory.shortTermTurns`,
  `rag.topK`/`minScore`, `persona.tone`/`warmth`/`emojis`/`forbiddenPhrases`/`maxLength`
  y el motor real de `ToolBinding.scopes`/`maxCallsPerConversation`/`requiresConfirmation`.
- Nuevo bloque `extensions` (personalización por tenant sin fork).
- `load_active_spec` tolerante: los specs v1 guardados siguen cargando (los campos
  eliminados se descartan, los nuevos toman default).

## v0.5.0 — Planes y facturación de tenants (2026-09-22)

Cada tenant opera 1 mes gratis y después se vincula a un plan (basic, premium,
empresarial) con cuotas y cobro por Wompi. Ver `docs/BILLING.md`, ADR-0045/0046,
HU-011.

### Planes y cuotas (ADR-0045)
- Catálogo de planes en código (`core/billing/plans.py`) espejado a la tabla de
  sistema `plans` (`sync_plans()` al arrancar). 4 planes con límites de clientes,
  conversaciones, audio, OCR, plugins, mensajes y costo LLM del período.
- `tenant_subscriptions` (RLS): todo tenant nuevo arranca en `free` con 30 días de
  trial; backfill de la migración `0034` para los existentes (30 días desde la migración).
- Medición derivada de las tablas fuente (`core/billing/metrics.py`): sin contador
  paralelo, consistente con `usage_ledger` (ADR-0008).
- Enforcement de bloqueo duro (`core/billing/enforcement.py`): tenant no operacional
  si el trial/período venció (webhook + consumer); cuota alcanzada bloquea la nueva
  unidad de la métrica (cliente, conversación, audio/OCR, pack, turno).
- Cron `core.billing_lifecycle` (diario) normaliza `trialing→expired`/`active→past_due`.

### Panel
- `/admin/plan`: plan actual, estado, días restantes, barras de progreso por métrica,
  planes disponibles e historial de pagos.
- Banners globales de cuota (80%/100%) y servicio detenido; resumen en el dashboard.
- `/admin/tenants` (master): plan de cada tenant + cambio de plan/ extensión de
  período con registro en `audit_log`.

### Cobro con Wompi (ADR-0046)
- `core/billing/providers/wompi.py` (cliente propio de la plataforma, replica el
  algoritmo probado de `pack-payments`): link de pago por período, firma de webhook
  verificada, correlación por `payment_link_id`, mínimo real de 150.000 COP.
- `POST /admin/plan/subscribe` + `POST /webhooks/billing/wompi` (firma + idempotencia).
- Débito recurrente automático: **diferido y documentado** (la interfaz queda lista).

### Infra / fixes de tests
- `.env.example`: bloque de billing (`BILLING_PROVIDER`, `WOMPI_BILLING_*`).
- Tests de integración: helper compartido `_meta_request` firma los webhooks con el
  App Secret de `.env` (antes fallaban 401 con el secret configurado); fixture
  `_reset_test_tenants_state` cierra conversaciones de los tenants de prueba para
  que una BD local reusada entre corridas no contamine los tests.

## v0.4.0 — BYOK de WhatsApp productivo (2026-09-22)

Deja el flujo BYOK (app propia de Meta) funcionando de punta a punta: mensaje entra →
LLM responde → llega al celular.

### Webhook y credenciales por tenant (ADR-0040)
- El webhook resuelve `verify_token` y `app_secret` por tenant
  (`ChannelConnection` manual → BYOK → `.env`); antes caían al `.env` y daban 403/401.
- El sender usa el `access_token` BYOK (`cloud_api_access_token`); ya no cae al `.env`.

### Asistente de token y expiración (ADR-0041)
- Wizard BYOK: "Extender a 60 días" (`fb_exchange_token`) y "Verificar" (`debug_token`);
  se persiste `expires_at`, badge en `/conexiones/salud` y cron diario de vencimiento.
- Mensaje claro para token vencido/inválido (`190/463`).

### Reconexión y crons de Meta
- Reconectar una app tras "eliminar" crea una conexión nueva y reasigna el
  `phone_number_id` (unique global) en vez de duplicar.
- Crons de Meta (`subscriptions_health`, `template_sync`, `pricing_sync`,
  `health_check`) enumeran cross-tenant vía funciones `SECURITY DEFINER`
  (migración `0033`, ADR-0039); antes eran no-op silencioso por RLS.

### Worker / Celery (ADR-0042, ADR-0044)
- Se registra `core.outbound_send` (`include`) y se reemplaza `asyncio.run()` por un
  puente async con **loop persistente** (`core/shared/async_bridge.py`).
- El beat escanea plugins (`beat_init`) y los crons de los packs se registran al importar
  el módulo del pack → `pack_commerce.follow_up_leads/quotes` quedan programados.

### Alta de tenant (ADR-0043)
- `/admin/tenants/nuevo` conecta WhatsApp por el flujo BYOK (MetaConnection +
  suscripción app/WABA); si falla, conserva el tenant y avisa.

### Infra
- La Funnel de Tailscale funciona tras reinstalar/configurar; **dominio estable pendiente**
  (ver `docs/BACKLOG.md`).

## v0.3.0 — Sprint 3 (2026-08-26)

### CULTO migrado (tenant #1)
- `crm_*` multi-tenant: migración `crm_0002_tenant` (tenant_id + RLS + policy en las 13
  tablas), UNIQUEs compuestos por tenant, **numeración de pedidos por tenant** (ADR-0018,
  `OC-0001` independiente por cliente), GRANT a `app_runtime`.
- Modelos del pack con `tenant_id` fail-fast (error claro sin scope); crons iteran tenants.
- Middleware por allowlist de rutas públicas: `/crm/*` y `/calendar/*` quedan con scope.
- Playbooks de CULTO a `SKILL.md` (ventas, seguimiento) + copy al cliente a `CRMConfig`
  editable (ADR-0017). Fix: `sync_database_url` usa el dueño para migraciones.

### Evals y CI
- Golden set de 35 escenarios del dominio CRM (camino feliz, objeciones, fuera de alcance,
  datos no disponibles, manipulación, escalamiento).
- Harness parametrizado por el spec de CULTO (`evals/specs/culto.json`) con reporte
  `(escenario, spec_version, git_ref)`; soporta subconjunto crítico.
- CI GitHub Actions (bloquea: ruff + pytest + gate de evals) + nightly (set completo).

### Canary y rollback (ADR-0016)
- Canary redefinido como gate + tenant staging + rollback 1 paso.
- `agent-core agent list|activate|rollback`: flip de `is_active` en un paso.

### Para quien usa esto
- `crm_*` ahora exige `tenant_scope` para insertar (error claro si no).
- Los mensajes de follow-up/confirmación/estados se editan en la configuración del pack.
- `agent-core db migrate` usa `MIGRATIONS_DATABASE_URL` (dueño) para migraciones.

**Rollback:** `git revert` + `alembic downgrade` de las migraciones 0005/0004/0003 y
`crm_0002`; `agent-core agent rollback` revierte el AgentSpec activo.

## v0.2.0 — Sprint 2 (2026-08-26)

### Runtime
- `AgentSpec` desde base de datos: tabla `agent_specs` (UNIQUE tenant+version, `is_active`,
  RLS), spec validada contra el contrato congelado, seed en BD. Cero prompts en código
  (ADR-0014): identidad/tono/reglas/respuestas viven en playbooks `.md` con allowlist.
- 8 tools nativas del core (skill `core`): `escalate_to_human`, `get_customer_profile`,
  `update_customer_profile`, `remember_fact`, `search_memories`, `load_playbook`,
  `get_conversation_usage`, `set_conversation_note`.
- Tool loop con límites (ADR-0015): `maxToolCallsPerTurn` y `budgetUsdPerConversation`
  desde el spec; presupuesto medido contra `usage_ledger` en la cabecera del turno; flag
  `budget_exhausted`; el core ya no se auto-desactiva.
- `usage_ledger` append-only (ADR-0008): una fila por turno con `agent_spec_version`,
  tools y resultado. Fuente única de costo.
- OpenTelemetry: span por turno con hijos `llm_call`/`tool_execute`, export OTLP por env.

### Panel
- `/admin/costos` sobre `usage_ledger` por tenant (filtro explícito + RLS) y por
  conversación, en vivo. Dashboard con filtros de tenant explícitos.

### Entidad de datos
- Tabla `agent_specs`, columna `usage_ledger.agent_spec_version`. Migración 0004 (con
  downgrade probado).

### Para quien usa esto
- Si tenías un tenant existente, el spec default se siembra/actualiza solo.
- `llm_usage` queda como fuente legacy (se retirará en Sprint 3).
- Config nueva: `MAX_CONCURRENT_TURNS`, `MAX_TURNS_PER_TENANT`, `OTEL_ENABLED`,
  `OTEL_EXPORTER_OTLP_ENDPOINT`.

**Rollback:** `git revert` + `alembic downgrade 0003` deja el esquema previo; las filas de
`usage_ledger`/`agent_specs` se descartan. Sin canary todavía (producción llega en Sprint 3).

## v0.1.0 — Sprint 1 (2026-08-26)

- Esquema multi-tenant: `tenant_id` + RLS real (rol `app_runtime`) en todas las tablas de
  negocio, `processed_events` (ADR-0007/0013), `usage_ledger`, `tenant`, `tenant_user`.
- Auth propia (ADR-0012): `tenant_scope` + `set_config` en transacción; resolución de
  tenant por phone_number_id.
- Gateway: dedupe, cuota de concurrencia por tenant, DLQ con reintento manual.
- 4 tests de salida de integración (dedupe, debounce, fairness, RLS).