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

**Confirmar el secreto de la plataforma antes de desplegar ADR-0100**
`connections.platform_app_secret` usa `WHATSAPP_APP_SECRET or META_APP_SECRET`, y desde ADR-0100 es la única llave
que autoriza eventos de cualquier negocio en el webhook genérico. Antes de desplegar, confirmar en el `.env` de
producción que no es el de la app de un cliente concreto (heredado del monolito). Además, Embedded Signup firma con
`META_APP_SECRET`: si los dos están definidos y son distintos, sus entregas al genérico dan 401. Decidir si la
plataforma es solo `META_APP_SECRET` (ADR-0094) y `WHATSAPP_APP_SECRET` queda como respaldo del endpoint por slug.

**La migración `crm_0009` falla con un negocio de 10.000 pedidos o más**
El backfill de `packages/pack-commerce/alembic/versions/0009_orders_numero_publico.py` arma
`'OC-' || lpad(order_seq::text, 4, '0')`, y `lpad` **trunca** a 4 caracteres: el pedido 10000 queda `OC-1000`, igual
que el 1000. El índice único `uq_crm_orders_tenant_number` aborta la migración y, con ella, la activación del pack
(`PluginError: Activación falló`). En local se reproduce con pedidos de prueba de `order_seq` ≥ 10000:
`test_admin_rbac`, `test_crm_playbooks` y `test_crm_tenant::test_numeracion_pedidos_por_tenant` fallan al activar
`pack_commerce`. Arreglo: en el backfill, `lpad` solo por debajo de 10000 y el número completo desde ahí (o una
migración nueva si `crm_0009` ya corrió en algún entorno). Visto el 2026-10-07 al correr la integración de
`feat/registrar-numero`.

**Otros que deciden por `status` sin seguir ADR-0101**
- `core/api/routers/connections.py:121` y `core/admin/views.py:1788` muestran `active = status == "connected"`: una
  conexión `degraded`/`error` que recibe y responde aparece como inactiva.
- `core/admin/meta_health_service.py:144`: las conexiones `degraded`/`error` desaparecen de la tabla de suscripciones,
  justo cuando la suscripción es el problema.
- `core/gateway/meta_preflight.py::resolve_app_credentials` no filtra por estado, no prefiere `origin = 'byok'` y
  ordena por `created_at`: puede devolver el `app_access` de una conexión `revoked` y elegir una conexión distinta de la
  que elige `_resolve_byok_token` (auditor-multitenant, 2026-10-06). Usar el mismo selector. (El filtro de
  `deleted_at` ya está, core #271.)
- Al tomar una conexión BYOK, el wizard de Embedded Signup (`meta_onboarding.exchange_code`) no revoca su
  `app_access`/`webhook_verify_token` ni desuscribe la app BYOK de la WABA. Meta sigue mandando eventos firmados por
  esa app; ADR-0101 los acepta, pero lo limpio es revocarlos y desuscribir.

- Resueltos por ADR-0122 (HU-CONN-03): `revoked` ya pausa (entrada sin IA, sin envío), el sender falla cerrado con
  `meta_connection_id` sin credenciales y "Activar" exige el diagnóstico verificado.
- Fuera de ADR-0122: los mensajes a un número pausado crean clientes y conversaciones que cuentan para la cuota, y
  pack-commerce marca un seguimiento como hecho aunque `send_to_conversation` devuelva `False` (se consumen durante
  una pausa).

**Lo que quedó fuera de ADR-0102 (la conexión se recupera sola)**
- `check_webhook_subscribed` solo pide que la WABA tenga alguna app suscrita, no la de la conexión. En BYOK,
  `own_app_subscribed` lo cubre con `WabaAccount.subscribed`, que es lo que se guardó al suscribir; no ve una
  desuscripción posterior hecha en Meta. Verificar el id de la app en `subscribed_apps`
  (`whatsapp_business_api_data.id`; comprobar antes la forma de la respuesta de Meta) lo cerraría.
- `check_app_subscription` solo mira `active`, no que el `callback_url` sea de este sistema ni que `fields` incluya
  `messages`. Una BYOK cuya app apunta a otro destino pasa el requisito de `next_status`. Exigir las dos cosas para que
  una BYOK mejore (aceptando la URL por slug y la genérica de ADR-0100). Auditor-multitenant, 2026-10-06.
- `meta_subscriptions_health._check_group` no persiste nada para un objeto que Meta omite en la respuesta, y
  `check_app_subscription` no mira la antigüedad de `last_checked_at`: un `active=True` viejo sobrevive aunque la
  suscripción ya no exista o el cron se haya parado. Desde ADR-0102 pesa más, porque `next_status` depende de él.
- Un alta BYOK interrumpida (reinicio del proceso o un `CancelledError`, que `except Exception` no captura) queda en
  `pending` con el número y quizá con `app_access`. En `_resolve_byok_token`, `pending` va con `connected` y gana por
  `connected_at`, y ningún cron revisa `pending`: le quita el secreto a la sana. Residuo de ADR-0101; un barrido que
  pase a `error` las `pending` viejas, u ordenarlas después de `connected`.
- El alta BYOK comprueba el token con el App Secret, pero no que el `app_id` cargado sea de ese secreto. Un negocio
  puede dar de alta una conexión con el `app_id` de otro: queda en `error`, entra al grupo de esa app en el cron de
  suscripciones (el respaldo de ADR-0102 evita que trabe la recuperación del dueño) y, si la activa, el panel le
  muestra la fila de `meta_app_subscriptions` del otro, con su `callback_url` (`/webhooks/whatsapp/<slug>`).
  Metadatos sin PII ni secretos. Arreglo: validar el par con `fetch_app_access_token(app_id, app_secret)` en el alta,
  rechazar `app_id == settings.meta_app_id`, y no mostrar un `callback_url` que no sea el del propio negocio
  (auditor-multitenant, 2026-10-06). Desde ADR-0102 el cron de suscripciones ya no revisa la app de la plataforma.
- La suscripción de la app de la plataforma no la revisa ningún cron: solo las conexiones BYOK guardan `app_access`.
  Si Meta la desactiva, todas las conexiones de Embedded Signup dejan de recibir sin aviso.
- `webhooks._alert_on_firma_ajena` repite a mano la deduplicación de `send_alert_once`; sus tests parchean
  `webhooks.get_redis`, por eso no se tocó.
- El candado horario del sync de plantillas es por `(tenant, waba_id)`, pero las plantillas se guardan por fila de
  WABA: si dos conexiones `connected` comparten la WABA, una de las dos no sincroniza y termina `degraded`.
- Sentry captura las variables locales por defecto y `_before_send` (`core/observability/sentry_setup.py`) solo
  tapa los tokens `EAA...`, no los App Secrets (32 hex). Una excepción inesperada en un cron que tiene un secreto en
  un frame lo mandaría al evento. `include_local_variables=False`, o tapar también los secretos de app. En
  `meta_subscriptions_health._group_connected_apps` se descifran los secretos de todas las conexiones de la app solo
  para compararlos: se podría comparar `MetaToken.token_hash` y descifrar solo el que se usa.
- `run_health_checks` toma la primera WABA de la conexión sin `ORDER BY`: con varias, `credentials_for`,
  `webhook_subscribed` y `templates` revisan una cualquiera. Elegir la del número vigente.

**Otras dos altas de conversación con la misma carrera que tenía el consumidor**
El consumidor ya serializa por cliente el get-or-create de la conversación (`pg_advisory_xact_lock`, 2026-10-06).
`core/gateway/outbound_email.py:72-100` y `core/admin/playground.py:59-96` siguen buscando y creando sin candado.
- En el correo, turnos concurrentes de pack-calendar (`pack_calendar/skills.py:978`) hacia el mismo buzón pueden
  dejar dos conversaciones abiertas. La primera alta del cliente puede soltar un `IntegrityError` crudo que
  `core/tools/core_skill.py:137` no captura, y pack-calendar lo cuenta como `error_envio`.
- En el playground, dos peticiones simultáneas del mismo negocio pueden crear dos conversaciones de prueba o dar
  un 500.

Afecta solo al mismo negocio. Arreglo: el mismo candado `conversation:{tenant}:{customer.id}` antes del `select`, y
`begin_nested` + relectura en el alta del cliente (auditor-multitenant, 2026-10-06).

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

**`graph_fetch` deja escapar errores de transporte de httpx**
`core/gateway/meta_graph.py:84-94` solo convierte `TimeoutException` y `ConnectError` en `GraphError`. Un
`ReadError`/`RemoteProtocolError` sube como excepción no controlada: la ruta da 500 y, con Sentry encendido, el evento
lleva los frames de httpx/httpcore con el cuerpo del pedido en bytes (p. ej. el PIN de `.../register`), que el
scrubber por clave no tapa. Arreglo: `except httpx.TransportError` → `GraphError(category="TRANSIENT")` sin
`http_status`. Hallazgo del `auditor-multitenant` sobre `feat/registrar-numero`, 2026-10-07.

**Follow-ups de auditoría diferidos (BYOK / token)**
- `core/gateway/meta_token_tools.py::extend_token` usa `httpx` directo → no queda en
  `graph_api_calls`/`graph_api_usage` (endpoint público de tokens, sin `appsecret_proof`).
- Rutas `POST /admin/conexiones/wizard/whatsapp-byok/token/inspect|extend` no validan que
  `app_id` pertenezca al tenant (en el alta inicial la conexión todavía no existe).
- `core/shared/events.py::emit_nowait` (fallback sync) no propaga el tenant (hoy sin callers). Desde
  ADR-0107/HU-SUB-03 los eventos de dominio ya no dependen de esto: van por el outbox `domain_events`
  con `tenant_id` explícito; el bus in-process queda solo para eventos del core.
- `meta_token_store.get_token` no filtra `tenant_id` en el WHERE (preexistente; el AAD falla
  cerrado).
- `ix_meta_tokens_tenant` no es índice compuesto (preexistente).
- `save_byok_connection` tiene un branch de "tenant mismatch" inalcanzable (RLS lo filtra antes).

**Pendientes tras HU-CONN-01 (credenciales por conexión, ADR-0094)**
- Suscripciones y webhook (`meta_subscriptions*.py`, `meta_preflight.resolve_app_credentials`,
  `connections.resolve_effective_app_secret`) siguen eligiendo "la" conexión del tenant en vez de usar
  `credentials_for`: es HU-CONN-05 (webhook y secreto por conexión).
- Tras una toma por el wizard de ES, `meta_connections.meta_app_id` sigue siendo el de la app BYOK anterior
  (ningún consumidor deduce el origen de él desde ADR-0094); alinearlo, y su índice único
  `uq_meta_connections_tenant_app`, va con HU-CONN-05.
- Descarga de media entrante: el core no la hace (`inbound.py` deja `url: ""`); HU-MEDIA-02 debe usar
  `credentials_for` para el `GET /{media_id}`.
- `meta_private.meta_tokens` tiene RLS habilitada pero **no forzada** (`relforcerowsecurity = f`): la migración
  `0029` solo recorrió el schema `public`. Hoy no hay fuga: el runtime (`app_runtime`) no es dueño, así que RLS
  le aplica, y `credentials_for` exige el `tenant_scope` de la conexión. Pero el AAD **no** frena una lectura
  sin scope de una conexión real (se arma con el `tenant_id` de la propia conexión), y el rol dueño lee la tabla
  sin política: incumple la invariante 1.
- Las migraciones asumen un rol con `BYPASSRLS` o superusuario: `0034` falla con un dueño sin bypass
  ("new row violates row-level security policy for table tenant_subscriptions"). La `0051` sí es segura con
  cualquier rol (fija `app.tenant_id` por tenant); la `0052` aborta con un mensaje claro si el rol no puede
  saltar RLS (sus funciones `SECURITY DEFINER` lo necesitan, ADR-0095).
- `tests/test_meta_byok.py` (7) y `tests/integration/test_api_meta_wizard.py::test_byok_*` /
  `test_meta_byok_reconnect.py` no stubbean `_token_expiry` → hacen una llamada real a Graph y fallan sin red.

**Pendientes tras ADR-0095 (un número o una WABA de un solo negocio)**
- Antes de desplegar la `0052` en producción: confirmar que `agent_core_migrations` (el `MIGRATIONS_DATABASE_URL`
  de producción en `docs/despliegue-coolify.md`) es superusuario o tiene `BYPASSRLS`; si no, la migración aborta el
  arranque a propósito. El mismo documento dice en sus notas que las migraciones corren como superusuario: alinear.
- `save_connection` para `whatsapp_meta` (panel clásico `/admin/conexiones/guardar`, alta de tenant, CLI) verifica
  el número contra Graph pero no a qué WABA pertenece. Con la regla de conflicto de ADR-0095 ya no entrega mensajes
  ajenos, pero es la vía de alta más débil: pedir la WABA y usar `verify_waba_and_phone`, o retirarla a favor de
  `POST /api/v1/admin/meta/manual`.
- `soft_delete_connection` (`core/admin/meta_health_service.py`) borra **físicamente** `phone_numbers` y
  `waba_accounts` de la conexión (y en cascada `message_templates`), contra la invariante 8. Lo cubre
  `tests/integration/test_meta_byok_numero_ocupado.py::test_eliminar_conexion_libera_numero_y_waba`. Las funciones
  de ruteo y de reclamo ya ignoran conexiones borradas, así que pasarlo a borrado lógico no rompe el ruteo.
- `resolve_tenant_for_page`, `resolve_tenant_for_ig_user` (migración `0021`) y la resolución de plantillas por
  `meta_template_id` (`0026`) siguen con `LIMIT 1` sin desempate. Hoy no hay clientes de Messenger ni Instagram;
  aplicar la misma regla (conexión viva, NULL si hay más de un dueño) cuando los haya.

**Pendientes tras ADR-0100 (firma por negocio en el webhook)**
- La respuesta del webhook dice si un número tiene dueño: un negocio que firma un grupo `(su WABA, número X)` recibe
  `"ignored"` si X es de otro (`tenant_conflict`, o `tenant_mismatch` por slug) y `"ok", "enqueued": 1` si X no
  tiene dueño (auditor-multitenant, 2026-10-05, preexistente). Responder con un cuerpo constante.
- `APP_ENV` vale `development` por defecto (`core/config.py`): un despliegue que olvide la variable deja activo el
  fallback que manda los eventos sin negocio (firmados por la plataforma) al primer negocio activo. Que su falta sea
  un error, o que el valor por defecto sea `production`.
- El wizard BYOK no impide que dos negocios carguen la misma app (`app_id`): con el mismo App Secret se autorizan
  entre sí en el webhook (se registra `webhook_secreto_compartido`). Rechazarlo en el alta, como el reclamo de
  ADR-0095.
- Retirar la rama de negocios del webhook genérico: re-registrar al endpoint por slug las suscripciones BYOK
  anteriores al wizard actual (`meta_subscriptions.upsert_app_subscription`) y, cuando el log
  `webhook_generico_firmado_por_negocio` llegue a cero, que el genérico solo acepte la firma de la plataforma.

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

**Suscripciones** (actualizado 2026-10-07 con ADR-0106 / HU-SUB-01)
- **Cartera recurrente (Opción 2, ADR-0082)** — `Cartera`/`Cuota` solo modelan un préstamo
  finito (`pack_cobranza/models.py:336-370`); no hay recurrencia indefinida. → se integra
  como **segundo proveedor** del contrato v1 de `core/subscriptions` en HU-SUB-15
  (préstamo rodante, D-SPEC-06-2). [Alta]
- **`pack_subscriptions` (Opción 1, ADR-0083)** — ✅ **existe desde el 2026-09-30**: pack
  con `Subscription`/`SubscriptionCharge` (planes como productos de commerce). Desde
  HU-SUB-01 implementa el contrato v1 (8 operaciones) y las tools `sub_*` viven en el
  core y despachan a él. [Cerrado]
- **Metadatos de recurrencia en productos** — `Product/ProductVariant` ya tienen
  `billing_period`/`trial_days` (se validan por enum en HU-SUB-10). [Cerrado]
- **Cobro recurrente con `pack_payments`** — `generar_link_pago` es one-shot
  (`pack_payments/skills.py:52-75`); el cobro por período con link ya existe
  (`charge_link` del contrato v1, con guard de período futuro); el débito recurrente
  sigue diferido (HU-SUB-17, Fase 2). [Media]
- **La suscripción y su evento se confirman en otra transacción que el pedido** —
  `activar_planes_del_pedido` (`pack_commerce/planes.py:103`) llama a `capability.activate`, y
  `crear_suscripcion_para` abre su propia sesión (`pack_subscriptions/capability.py:124`): la
  suscripción y `subscription.activated` quedan confirmadas aunque la transacción del pedido se
  revierta después (p. ej. un error al registrar la promoción en `crear_pedido`,
  `pack_commerce/skills.py:1499`). El alta automática lo tolera (exige el pedido visible y pagado;
  si no aparece en 3 intentos queda en revisión), pero la suscripción huérfana queda. → que el
  proveedor use la sesión del llamador o que commerce active después del commit. Visto en HU-SH-03
  (2026-10-09). [Media]
- ✅ **[Cerrado por HU-SH-04, ADR-0123: el plan cambia en el mismo negocio con su preset, se renueva y se paga por los dos canales]** ~~Un cambio de plan desde el asistente no cambia el plan del negocio aprovisionado~~ — Free →
  Premium cambia la suscripción en `solvershub`, pero el consumidor de ADR-0121 solo lo audita
  (`aprovisionamiento_cambio_de_plan`): el negocio sigue en Free y vence a los 30 días. Tampoco aplica
  el preset que quedó pendiente porque no cabía en Free (ADR-0110 § 3.1). El plan pago aprovisionado
  tampoco se renueva al vencer `period_end`. → HU-SH-04 (fuente única del cobro de plataforma, renovación
  y preset pendiente al subir de plan). Hasta entonces, no encender `PROVISIONING_SELLER_TENANTS` en
  producción. [Alta]
- **Rotar número y correo sigue permitiendo un Free tras otro** — el alta automática (ADR-0121) manda a
  revisión un Free del mismo cliente o del mismo correo, pero quien cambia de número de WhatsApp y de
  correo en cada alta sigue creando negocios Free. → HU-SH-07 (antiabuso: documento, verificación y
  límites por origen). [Alta]
- **Confirmar el correo en el chat antes de crear el pedido de un plan** — el acceso del negocio
  aprovisionado llega solo al correo del pedido (ADR-0121): si el cliente lo escribe mal, se queda sin
  acceso hasta que una persona lo corrija (`docs/runbooks/altas-en-revision.md`). Es un ajuste del playbook de
  ventas del tenant `solvershub` (`solvershub-comercial`): repetir el correo y pedir confirmación antes de
  `crear_pedido`. [Media]
- **Reactivar el negocio de quien vuelve a comprar después de cancelar** — desde HU-SH-04 (ADR-0123 § 12)
  el alta queda en revisión en vez de crear un segundo negocio, y una persona reactiva el que tenía. →
  reactivarlo solo (misma suscripción nueva vinculada al negocio anterior). [Media]
- **Cambio de plan desde `/plan` para un negocio del asistente** — HU-SH-04 lo bloquea (409
  `cambio_en_el_asistente`, ADR-0123 § 6): cambiar el plan en el vendedor sin cobrarlo de nuevo exige otra
  operación del contrato, y el prorrateo es de HU-BILL-12. [Media]

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
- **Embudo de activación en el panel** (HU-SH-06, ADR-0124) — hoy se mide con `agent-core tenant embudo`
  y el log `onboarding_hito`. Si se quiere un tablero o alertas, un cron que barra las altas recientes y
  una vista agregada para el master. [Baja]
- **Enlace de invitación vencido de una cuenta sin contraseña** — si el owner no usa el enlace en 7 días,
  «olvidé mi contraseña» le define la contraseña, pero la invitación vencida ya no se muestra: entra sin
  negocio. Hace falta reenviar la invitación (master) o renovarla al recuperar la contraseña. Visto en
  HU-SH-06 (2026-10-09). [Media]
- **`brand_voice` vacío no se detecta** — `AppSettings.brand_voice` puede quedar vacío sin
  aviso, y es la fuente de verdad de precios. → validación/aviso en el panel. [Baja]
