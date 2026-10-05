# Backlog técnico

Hallazgos verificados que **no** están arreglados. Cada uno con su evidencia, para que
no haya que volver a descubrirlos. Origen: auditoría del repo de 2026-08-25.

Cuando arregles uno, bórralo de aquí y menciónalo en el commit.

---

## Alta

**No hay dominio público estable para el webhook de Meta**
La app `agent-core-test` de Coolify solo tiene el dominio *default*
`http://<app-id>.186.82.102.11.sslip.io` (**HTTP, sin TLS**), y los puertos **80/443 del
host están cerrados/filtrados** desde internet → Coolify/Traefik no puede servir HTTPS ni
validar Let's Encrypt (los dominios Coolify existentes también están caídos:
`test.formotos.com` → 521, `app.formotos.com` → 000). Por eso el repo usa la **Funnel de
Tailscale** (outbound), pero su **DNS público se cayó** (`NXDOMAIN` en resolvers públicos)
hasta reiniciar `tailscaled` → Meta no puede verificar ni entregar. **Decisión pendiente:**
Cloudflare Tunnel (`agentcore.formotos.com`, outbound, requiere `cloudflared tunnel login`
en la cuenta dueña de `formotos.com`) vs abrir 80/443 y usar el dominio de Coolify.

**Los costos de Anthropic siempre se registran como 0.0**
`core/orchestrator/loop.py:_record_usage` importa el diccionario `PRICING` de
`core/llm/openai_compatible.py`, que solo contiene modelos de DeepSeek y OpenAI. Los
modelos de Claude no están, así que su costo cae a cero. Existe
`BaseLLMProvider.estimate_cost_usd()` (`core/llm/base.py:37-41`) precisamente para
esto, pero nadie la llama. El panel `/admin/costos` subestima el gasto real.

**La firma del webhook es opcional en la práctica**
`core/gateway/webhooks.py:65` solo verifica el HMAC `X-Hub-Signature-256` si hay
`app_secret` configurado. Sin él, cualquiera que conozca la URL puede inyectar
mensajes. Debería fallar cerrado, o al menos gritar en los logs.

**El pedido de monto alto nunca escala**
`plugins/pack_commerce/pack_commerce/skills.py:989` devuelve `{"escalate": True}`
cuando el monto supera `monto_alto_threshold`. Ningún punto de `core/` lee esa clave:
`core/orchestrator/safe_renderer.py` solo interpreta `_safe_append` e `_image_url`.
La señal se pierde y el LLM solo ve un error genérico.

## Media

**El system prompt promete una herramienta que no existe**
`core/llm/prompt_builder.py:BASE_RULES` dice *"si el cliente pide hablar con una
persona, usa la herramienta de escalamiento"*. No hay ninguna tool de escalamiento
registrada. El escalamiento ocurre por keywords, guardrails, refusal o max_iterations,
nunca por decisión del LLM. `MCPHost.trigger_escalation()` existe
(`core/mcp/host.py:80`) y ningún plugin la llama.

**`BASE_RULES` del core menciona el CRM**
Mismo archivo: el prompt del framework habla de "registrar el lead en el CRM", que es
un plugin concreto. Acopla el core a `pack_commerce`.

**El contenedor corre como root y ensucia el repo del host**
El `Dockerfile` no crea usuario no-root, y `docker-compose.yml` bind-montea
`./plugins`. Al arrancar, el entrypoint hace `pip install -e` de cada plugin y deja
`*.egg-info/` y `__pycache__/` **propiedad de root** dentro del repo del usuario, que
después no puede borrarlos ni reinstalar los plugins desde el host sin `sudo`.
Verificado: tras un `docker compose up`, `rm -rf` sobre el clon falla con
*Permission denied* en `plugins/*/`.
Arreglarlo bien implica un usuario no-root en la imagen con cuidado de que
`pip install -e` siga funcionando sobre site-packages.

**Panel admin sin CSRF ni rate limiting**
Todos los POST de `core/admin/views.py` son formularios sin token. `/admin/login` no
tiene límite de intentos. No hay middleware de seguridad de ningún tipo (el `grep` de
`add_middleware` en `core/` no devuelve nada). `itsdangerous` está en las dependencias
y no se usa.

**`Vector(384)` hardcodeado**
En `core/db/models.py` y en la migración `0001_initial_schema.py`, mientras
`EMBEDDING_DIMENSIONS` es configurable en el `.env`. Usar `text-embedding-3-small`
(1536) exige editar el código y escribir una migración a mano. El docstring de
`core/memory/vector.py:14` lo reconoce.

**Embeddings por tenant (BYOK)**
Hoy `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL` son globales del despliegue. Que cada
tenant traiga su proveedor de embeddings exige claves por tenant + dimensión por
proveedor (amarrado al punto anterior). Diferido en ADR-0053.

**63 avisos de longitud de línea**
`E501` está excluido en `pyproject.toml` porque activarlo hoy exigía un reformateo
masivo mezclado con el arreglo del arranque. Abordar aparte, en una rama solo de
formato.

**`mypy` no puede fallar**
`make lint` corre `mypy core cli || true`, lo que anula el `strict = true` declarado en
`pyproject.toml`. Medir primero el volumen (`mypy core cli | tail -1`) y activarlo por
módulos con `[[tool.mypy.overrides]]` antes que de golpe.

**`mod-media-processor` no existe**
`cli/registry.py:25` lo anuncia como plugin conocido y
`core/gateway/webhooks.py:116-123` engancha con él para procesar audio e imagen. No
está en el repo ni en ninguna doc. Sin él, todo mensaje con media recibe
*"[El cliente envió un archivo multimedia que no puedo procesar]"*.

**`plugin scaffold` genera menos de lo documentado**
`docs/PLUGIN-CONTRACT.md` §3 promete `models.py`, `jobs.py`, `alembic.ini` y
`alembic/`. `cli/commands/plugin.py:197-307` genera 5 archivos y ninguno de esos —
justo la parte delicada (el `version_table` separado del ADR-005). La misma promesa se
repite en `sdd/templates/skills-config.md.j2` §4 y `.opencode/agents/plugin-dev.md`.

## Baja

**Follow-ups de auditoría diferidos (BYOK / token)**
- `core/gateway/meta_token_tools.py::extend_token` usa `httpx` directo → no queda en
  `graph_api_calls`/`graph_api_usage` (endpoint público de tokens, sin `appsecret_proof`).
- Rutas `POST /admin/conexiones/wizard/whatsapp-byok/token/inspect|extend` no validan que
  `app_id` pertenezca al tenant (en el alta inicial la conexión todavía no existe).
- `core/shared/events.py::emit_nowait` (fallback sync) no propaga el tenant (hoy sin callers).
- `meta_token_store.get_token` no filtra `tenant_id` en el WHERE (preexistente; el AAD falla
  cerrado).
- `ix_meta_tokens_tenant` no es índice compuesto (preexistente).
- `save_byok_connection` tiene un branch de "tenant mismatch" inalcanzable (RLS lo filtra antes).

**Pendientes tras HU-CONN-01 (credenciales por conexión, ADR-0094)**
- Suscripciones y webhook (`meta_subscriptions*.py`, `meta_preflight.resolve_app_credentials`,
  `connections.resolve_effective_app_secret`) siguen eligiendo "la" conexión del tenant en vez de usar
  `credentials_for`: es HU-CONN-05 (webhook y secreto por conexión).
- Descarga de media entrante: el core no la hace (`inbound.py` deja `url: ""`); HU-MEDIA-02 debe usar
  `credentials_for` para el `GET /{media_id}`.
- `POST /admin/meta/manual` valida el token con las credenciales del tenant (o la plataforma) pero la conexión
  `manual` se resuelve siempre con la app de la plataforma: un token de la app BYOK del negocio pasaría la
  validación y fallaría después por `appsecret_proof`.
- `meta_private.meta_tokens` tiene RLS habilitada pero **no forzada** (`relforcerowsecurity = f`): la migración
  `0029` solo recorrió el schema `public`. Hoy no hay fuga (el runtime no es dueño y el AAD falla cerrado),
  pero incumple la invariante 1.
- Las migraciones asumen un rol con `BYPASSRLS` o superusuario: `0034` falla con un dueño sin bypass
  ("new row violates row-level security policy for table tenant_subscriptions"). La `0051` sí es segura con
  cualquier rol (fija `app.tenant_id` por tenant).
- `tests/test_meta_byok.py` (7) y `tests/integration/test_api_meta_wizard.py::test_byok_*` /
  `test_meta_byok_reconnect.py` no stubbean `_token_expiry` → hacen una llamada real a Graph y fallan sin red.

**Código muerto**
`MCPHost.register_resource()` y `MCPResource` (registrados, nunca leídos);
`BaseLLMProvider.stream()` (implementado en ambos providers, el orchestrator nunca lo
invoca); `core/tasks/queue.py` completo (importado por cero archivos);
`MemoryError_` y `GuardrailViolation` en `core/shared/exceptions.py`;
`working_memory.clear()`; `entity.find_by_external_id()`; `connections.get_connection()`;
el evento `ON_CONVERSATION_CLOSED` (declarado, nunca emitido — `conversations.closed_at`
no se escribe nunca).

**El consumer no reclama mensajes pendientes**
`core/orchestrator/consumer.py` usa `CONSUMER = "orchestrator-1"` hardcodeado y no hay
`XAUTOCLAIM`/`XPENDING`. Si un proceso muere entre `xreadgroup` y `xack`, esa entrada
queda pendiente para siempre. Con varias réplicas, todas comparten nombre de consumidor.

**Prompt caching de Anthropic: un breakpoint desperdiciado**
`core/llm/anthropic_provider.py:55-61` marca `cache_control` en **todos** los bloques
de system, incluido el dinámico, que cambia cada turno. Ese breakpoint escribe caché
que nunca se reutiliza. `docs/PROMPT-CACHING-GUIDE.md` describe el comportamiento
deseado (no cachear el bloque 4) y dice "usamos 3, queda 1 libre", que no es lo que
hace el código.

**Documentación con deriva**
- `docs/PLUGIN-CONTRACT.md` §13 prohíbe importar `core.plugin_manager`, y los dos
  plugins de referencia lo importan (`pack_commerce` en `module.py:76`, `skills.py:80`,
  `jobs.py:39`; `pack_calendar` en `module.py:32,50`). La lista blanca tampoco
  menciona `core.admin.auth`, `core.config`, `core.db.models` ni `core.shared.crypto`,
  que ambos usan.
- `docs/FIRST-ASSISTANT.md` paso 9: tutorial de barbería que pide "una hamburguesa".
- `docs/MONOLITH-MODEL.md` paso 5 dice reiniciar solo `core-api`; el código reinicia
  tres servicios (`cli/commands/plugin.py:170-175`).
- Los 82 criterios de aceptación de `docs/HU/` están sin marcar aunque la
  funcionalidad está implementada.
- `sdd/.state.json` está commiteado con datos de una prueba ("Barber Bot Test", CRM
  v1.0.0, `skills: []`) que no corresponden al CRM v2 actual.
- `sdd/templates/skills-config.md.j2` y `architecture.md.j2` llevan valores del
  framework escritos a mano ("máx 6 iteraciones", "L1 Redis 30 turnos", la lista de 11
  tablas del core) que no se parametrizan desde el estado.

**Sin `LICENSE`**
`README.md` y `pyproject.toml` declaran MIT; el archivo no existe.

---

## C4 review (2026-08-28)

Hallazgos del diagrama C4 (`docs/architecture/c4.md`). Los ya resueltos están
marcados; los abiertos siguen aquí.

**La observabilidad está gated por env**
`core/observability/tracing.py` configura el exporter OTLP solo si `OTEL_ENABLED`;
por defecto el tracer es no-op. El panel de costos lee `usage_ledger` (funcional),
pero no hay trazas del loop/consumer en producción. → Activar el exporter OTLP y
exponer métricas (p99 del webhook) cuando se despliegue producción real. [Media]

**Consentimiento de scopes fail-open transitorio (ADR-0019)**
`_tool_denial` (`core/orchestrator/loop.py`) niega solo si hay `ToolBinding.enabled=false`
o el pack no está habilitado; sin binding la tool corre (fail-open). Los bindings
(`AgentSpec.tools`) de CULTO/FORMOTO no están migrados. → Migrar los bindings de las
tools sensibles y pasar a fail-closed. [Media]

**Estructura declarada ≠ realidad (resuelto)**
`CLAUDE.md` ya refleja el monolito en `core/` y los packs en `packages/`; los stubs
`apps/*`, `core-crm`, `core-runtime`, `channel-whatsapp` quedan documentados como
hogares futuros. [Baja — resuelto]

**`plugins/` migrado a `packages/` (resuelto)**
`mod_google_calendar` → `packages/pack-calendar` (id `pack_calendar`). `plugins/` ya
no existe. [Baja — resuelto]

**Topología panel vs gateway+consumer (decisión)**
Ver ADR-0021: hoy un solo proceso; se separa cuando OTel muestre p99 del webhook
degradado. [Media — decidido, sin código]

**Vistas del CRM por descriptor (Fase D)**
El renderizador por descriptor (`core/admin/view_registry.py`) sirve las vistas
con HTML genérico; leads ya está portado. Las vistas complejas del CRM (productos
con árbol de categorías, promociones, pedidos con line_items) siguen en plantillas
propias del pack. → Migrarlas a descriptores cuando el descriptor soporte los casos
(tabla con árbol, sub-items). [Media — follow-up Fase D]

---

## Asistente SolversHub (2026-09-30)

Requerimientos para que el asistente del tenant `solvershub` venda planes, gestione
**carteras como suscripciones** y **agende demos** sin alucinar. ADRs: 0082 (cartera
recurrente), 0083 (`pack_subscriptions`), 0084 (calendario interno), 0085 (persistencia
de artefactos de tenant).

**Suscripciones**
- **Cartera recurrente (Opción 2, ADR-0082)** — `Cartera`/`Cuota` solo modelan un préstamo
  finito (`pack_cobranza/models.py:336-370`); no hay recurrencia indefinida ni tools para
  crear/renovar/cancelar. → `Cartera.modalidad`, ciclo/estado, cron de cargos por período y
  tools `cob_*sus*`. [Alta]
- **`pack_subscriptions` (Opción 1, ADR-0083)** — no existe suscripción de cliente final
  sobre planes; hoy los planes se “simulan” como productos. → pack opcional con
  `Plan`/`Subscription`/`SubscriptionCharge` y tools `sub_*`. [Alta]
- **Metadatos de recurrencia en productos** — `Product/ProductVariant` no tienen
  `billing_period`/`trial_days`/entitlements; el asistente no puede informar plan/prueba sin
  inventar. → añadir metadatos + tool `sub_informar_planes`/`informar_plan`. [Media]
- **Cobro recurrente con `pack_payments`** — `generar_link_pago` es one-shot
  (`pack_payments/skills.py:52-75`); no hay cargo periódico ni reintentos. → cobro por
  período con link (base) y débito recurrente (diferido, `ROADMAP.md:150-175`). [Media]

**Calendario / demos**
- **Calendario interno (ADR-0084)** — las 5 tools requieren `google_calendar`
  (`pack_calendar/skills.py:100,116,157,178,212`); sin OAuth no se agenda. → modo
  `interno` con `BusinessHours`/`CalendarBlock` y tools sin Google. [Alta]
- **`check_availability` sin slots libres** (`skills.py:599-634`) — devuelve ocupados y la
  ventana, pero el LLM debe razonar los huecos → alucina horarios. → calcular `free_slots[]`.
  [Alta]
- **`create_calendar_event` sin duración/tipo** (`skills.py:217-265`) — usa siempre
  `event_duration_minutes`; no acepta `appointment_type_id`. → parámetros + tipos. [Media]
- **Flow default de calendar incompleto** (`pack_calendar/flow.json:9`) — solo
  check/create/send_email; update/cancel/list no llegan al LLM. → ampliarlo + tool
  `list_appointment_types`. [Media]
- **Seed de citas/tipos** — `seed_catalog` solo cubre `pack_commerce`
  (`cli/commands/tenant.py:230-353`). → seed genérico por pack. [Media]

**Framework / plataforma**
- **Artefactos de tenant efímeros (ADR-0085)** — el contenedor de Coolify no monta
  `TENANTS_ROOT`; `flow.json`/playbooks/catálogo se pierden en el redeploy (solo `brand_voice`
  persiste en `AppSettings`). → volumen persistente. [Alta]
- **Evals no aplican el flow resuelto** — `evals/runner.py:181` pasa todas las skills del
  `mcp_host`; un escenario pasa aunque el `flow.json` del tenant excluya la tool → los evals
  no protegen contra ese hueco. → usar `resolve_flow` en el runner. [Media]
- **`completion_claims` no parametrizables en calendar** — `pack_calendar/module.py:39-72`
  las tiene fijas (commerce sí las configura, `pack_commerce/config.py:115-136`). → DSL
  por tenant. [Baja]
- **`brand_voice` vacío no se detecta** — `AppSettings.brand_voice` puede quedar vacío sin
  aviso, y es la fuente de verdad de precios. → validación/aviso en el panel. [Baja]
