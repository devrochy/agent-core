# Changelog

## Sin versión — Una conexión BYOK degradada sigue recibiendo mensajes (ADR-0101, 2026-10-06)

- **Bug (alto, preexistente):** cuando el cron de salud pasaba una conexión BYOK a `degraded` o `error`,
  `_resolve_byok_token` dejaba de leer su App Secret y su `verify_token`. Todo el inbound del negocio daba 401
  (por slug y por el genérico) y el self-check de la suscripción fallaba, así que la conexión no se recuperaba.
  Ahora esos estados siguen prestando la credencial; `revoked` (desactivada a mano o borrada) y `draft` no.
  Si hay varias, las sanas van antes que las de `error`.

## Sin versión — Una ráfaga de mensajes no parte la conversación en dos (2026-10-06)

- **Bug (preexistente):** el consumidor procesa cada mensaje del batch en su propia tarea, y
  `_resolve_conversation` buscaba la conversación abierta del cliente y, si no había, la creaba sin candado.
  Varios mensajes seguidos de un cliente sin conversación abierta podían crear una conversación cada uno, y el
  agente los atendía en turnos separados, sin el contexto de los otros. Ahora el get-or-create se serializa por
  cliente con `pg_advisory_xact_lock` hasta el commit, como `meta_asset_claims` (ADR-0095). Sin migración.
- Test: `tests/integration/test_consumer_conversacion_concurrente.py` (5 tareas a la vez sobre un cliente sin
  conversación abierta: sin el candado salen 5 conversaciones).

## Sin versión — Un evento del webhook solo actúa sobre un negocio autorizado por su firma (ADR-0100, 2026-10-05)

- **Seguridad (alto, preexistente):** el webhook genérico aceptaba la firma con el secreto de cualquier negocio del
  payload y procesaba todas las entries: un negocio BYOK podía inyectarle mensajes a otro. Ahora cada grupo
  (entry, número) se procesa solo si lo firmó la plataforma (vale para todos) o su propio negocio; si no, queda en
  `webhook_events` como `signature_tenant_mismatch` (`signature_ok=False`) y se alerta. El POST se rutea una sola
  vez (`_rutear_grupos`), con la misma resolución que el despacho.
- Los eventos de plantillas y de estado de mensajes reciben el negocio de su grupo (`expected_tenant_id`) y se
  descartan si la plantilla o el mensaje es de otro: con su propia firma, un negocio ya no puede cambiar una
  plantilla ajena ni el estado o los costos de los mensajes de otro (genérico y por slug). El estado se busca
  dentro del negocio de la entry; `resolve_tenant_for_message` (`LIMIT 1`) queda solo para el log.
- `connections.platform_app_secret` y `resolve_own_app_secret`; `resolve_effective_app_secret` no cambia.
- El cron de salud ya no cuenta los eventos rechazados como "worker caído" (alertaba cada hora para siempre).
- La alerta "Un negocio firmó eventos de otro" sale una vez cada 24 h por par; marcar un evento como procesado
  filtra por su negocio; un payload que no es un objeto JSON se ignora con 200.

## Sin versión — Renumeración de migraciones y ADRs de la app (2026-10-05)

- **CI de `develop` en rojo:** los PRs de la app (HU-APP-02/05/14/15) se mergearon con las migraciones `0051` y
  `0052` que ya usaban HU-CONN-01 y ADR-0095, y `alembic upgrade head` fallaba por varias cabezas. Las de la app
  pasan a ir después de las de Meta: `0053_password_reset_tokens`, `0054_push_tokens` y
  `0055_mobile_audit_events`. Solo cambian los números y el orden: el esquema final es el mismo.
- Los ADRs de la app que repetían número pasan al final: recuperación de contraseña y token de gestor a ADR-0098
  (era 0094) y notificaciones push a ADR-0099 (era 0095). Búsqueda global (0096) y sync (0097) no cambian; los
  cuatro entran al índice de `docs/adr/`.

## Sin versión — Un número o una WABA de Meta pertenecen a un solo negocio (ADR-0095, 2026-10-05)

- **Seguridad (alto, preexistente):** un negocio podía registrar la WABA o el número de otro y recibir sus
  mensajes. El webhook ahora rutea **primero por el número** y después por la WABA. Si el número es de un negocio
  y la WABA de otro, o el número (o la WABA que decide) tiene dos dueños, el evento no va a ninguno: queda
  `tenant_conflict` y nunca cae al negocio del endpoint por slug ni al de desarrollo. Cada grupo
  `(entry, número)` se rutea por separado.
- `resolve_tenant_for_phone` y `resolve_tenant_for_waba` (migración `0052`) solo cuentan conexiones vivas (no
  borradas y fuera de `draft`; `resolve_tenant_for_phone` también mira `phone_numbers`, y en `channel_connections`
  solo WhatsApp por Meta) y devuelven NULL si el activo lo reclaman dos negocios. Dejan de ser ejecutables por
  PUBLIC y fijan `search_path = public, pg_temp`. La migración aborta si el rol no puede saltar RLS, lista en el
  log los activos con dos dueños y los de altas manuales a revisar, y retira (borrado lógico, reversible por la
  marca en `status_reason`) las conexiones en `draft` con filas del Embedded Signup anterior, cerrando su wizard.
  "Activar" ya no saca una conexión de `draft`.
- Función nueva `meta_asset_claimed_by_other_tenant` (`SECURITY DEFINER`, solo `app_runtime`): responde si otro
  negocio ya tiene la WABA o el número, sin decir cuál. Sincronizar o agregar números no guarda un número de otro
  negocio (`meta_numero_de_otro_negocio_no_se_guarda`); sincronizar sigue con los demás.
- **Altas verificadas** (`core/gateway/meta_asset_claims.py`): la manual, BYOK y Embedded Signup comprueban en
  Graph que el token ve la WABA y que el número es de ella, y rechazan (`asset_claimed`) un activo de otro
  negocio, con un candado por activo en la misma transacción del insert. También `onboard_tenant` y
  `save_connection` (tipo `whatsapp_meta`).
- **Embedded Signup:** `POST /api/v1/admin/meta/wizard/exchange` ya no guarda la WABA ni el número antes del canje
  (un `code` inventado los dejaba escritos); se guardan después, verificados con el token nuevo. Un `code`
  inválido responde 400 `meta_error` en vez de 500.

## Sin versión — Credenciales de Meta por conexión (HU-CONN-01, 2026-10-05)

- **`meta_connections.origin`** (`byok` | `embedded_signup` | `manual`, `NOT NULL` sin default, migración
  `0051`): el modo de credenciales vigente de la conexión. Lo fija cada alta (y cambia si el wizard de ES toma
  una conexión BYOK); deja de inferirse por los tokens que existen. Backfill por tenant (fija `app.tenant_id`)
  con la evidencia de cada alta; con tokens de los dos orígenes queda `byok` (el que usaba el sender) y se
  listan en el log; sin evidencia → `manual`. ADR-0094.
- **Una sola regla de qué token usa cada origen**, `core/gateway/meta_credentials.py`: `access_token_refs`,
  `access_token_for`, `access_token_expires_at` y `credentials_for` (`MetaCredentials`). BYOK usa el token y el
  App Secret de la conexión; Embedded Signup y manual, el token de la conexión y la app de la plataforma. Sin
  credenciales → `CredentialsUnavailable(reason)`; sin `tenant_scope` activo → `no_tenant_scope`.
- Números, Plantillas (panel SSR y API), salud, costos/créditos, endpoints de token BYOK y los crons de
  plantillas y de precios usan las credenciales de **esa** conexión. Antes casi todo asumía Embedded Signup:
  fallaba con BYOK y los crons exigían `TOKEN_KIND` **y** `app_access`, así que no procesaban ninguna conexión.
- **El sender y los avisos de vencimiento** (cron diario, resumen de salud, salud del panel SSR e `inspect`)
  usan el token que dice `origin` en vez de "BYOK primero": una conexión BYOK tomada por ES ya no envía con el
  token viejo ni dispara alertas críticas falsas por él (supersede parcial de ADR-0040 §3 y ADR-0041 §5).
- La columna "Origen" de la lista de conexiones sale de `origin` ("BYOK (app propia)", "Embedded Signup",
  "Carga manual"), no de comparar `meta_app_id` con la app de la plataforma.
- `POST /api/v1/admin/meta/manual` valida el token con la app de la plataforma, la misma con la que se resuelve
  (enmienda ADR-0034); sin ella configurada, 400 `platform_app_not_configured`.
- Crons: el error se registra por conexión (`connection_id`, `tenant_id`, `reason`) y no corta a las demás; el de
  plantillas pide las credenciales antes del lock horario, y el lock lleva el tenant en la llave. La
  sincronización manual de precios ahora informa las conexiones sin credenciales (antes las omitía en silencio).
- `POST /admin/meta/byok/token/extend` responde 400 `not_byok` para conexiones que no son BYOK (antes de
  resolver credenciales).
- Se elimina `resolve_waba_and_token` (y su copia en el panel SSR) a favor de `resolve_waba_and_credentials`.

## Sin versión — Conectar Google Calendar desde el panel (HU-CAL-07, 2026-10-05)

- El retorno OAuth de Google entra por el panel: `redirect_uri` = `<PANEL>/api/calendar/oauth/callback`
  (antes una ruta inexistente de la API → `redirect_uri_mismatch`/404). ADR-0093.
- `/oauth/start` genera un `state` anti-CSRF (cookie httponly `oauth_state`); `/oauth/callback` exige
  sesión (`pack_calendar:conexion`), valida el `state`, guarda los tokens en la conexión del negocio
  del principal y borra las cookies del flujo.

## Sin versión — Pack de agendamiento definitivo (HU-CAL-05, 2026-10-05)

- `check_availability`: fecha pasada → `status: in_past` (con `today`); para hoy no
  ofrece horas que ya empezaron; agrega `status`/`today` a la respuesta.
- Tool nueva **`find_next_available`** (`days` 1-14): próximos días con horarios
  libres, sin días cerrados ni sin cupo, máximo 5.
- **Link "agregar a mi calendario"** (plantilla de Google Calendar) en create/update
  del modo interno y en el correo (`{link}`, con línea de respaldo si la plantilla no
  lo trae). Plantillas de correo/WhatsApp con formato seguro.
- **Guardrail con evidencia real** (`status` del turno, D-SPEC-24-8): `rejected`/
  `not_found` ya no respaldan confirmaciones; "moví tu demo" cubierto; las preguntas
  no disparan el guardrail; el correo se respalda con `email_sent` y el reintento
  nunca pide `send_email`.
- **Panel**: mover o cancelar con `notify` avisa por correo y WhatsApp y devuelve
  `notification`; cancelar registra `cancelada`, es idempotente y responde 200.
- `citas.md` y propósitos de `flow.json` definitivos.

## Sin versión — Fecha actual en el prompt y anti-escalamiento (HU-CAL-04, 2026-10-04)

- **Zona horaria del negocio** `app_settings.timezone` (migración `0050`, default
  `America/Bogota`), editable en Configuración → Identidad; zona inválida → 422 y un
  PUT sin el campo no la pisa. El horario de atención la usa si no trae zona propia
  (antes caía a `UTC`). ADR-0091.
- **Fecha y hora actual en el prompt**: última parte del bloque dinámico, solo el dato
  ("Hoy es martes, 6 de octubre de 2026 — 10:00 (America/Bogota)."); la instrucción de
  cómo usarla y la regla de no filtrar los `[SISTEMA]` viven en `reglas-operacion.md`.
- **Respuesta neutral al agotar el guardrail** (`behavior_config.guardrail_fail_action`
  = `escalate` | `neutral`, default `escalate`): con `neutral` responde la sección
  `verifying` de `respuestas.md`, no escala, registra `audit_log` `guardrail_neutral` y
  cuenta el turno como no resuelto (el autoescalado por turnos sigue de red). ADR-0092.
- `guardrail_max_retries` se valida 0-5 en el API, igual que en el loop.

## Sin versión — Playground "Probar asistente" + F0 del canal waweb (2026-09-27)

### Playground (ADR-0075)
- Nueva vista `/admin/asistente/probar`: conversa con el asistente del tenant
  **sin pasar por ningún canal** (mismo loop del runtime, `process_conversation`).
- Botón destacado **"Probar asistente"** (acento indigo propio) en `/admin/conexiones`
  y en el menú lateral; vista tipo celular con aviso de modo prueba.
- Conversación de prueba `channel == "test"`: **no** persiste filas en `messages`,
  queda excluida de las métricas de billing (`clientes`/`conversaciones`/
  `costo_llm_usd`) y de `escalate()` (no crea `Escalation`, no pausa, no alerta).
- Archivos: `core/admin/playground.py`, `core/shared/test_mode.py`,
  `core/admin/templates/asistente_probar.html`, rutas en `core/admin/views.py`,
  filtros en `core/billing/metrics.py` y `core/orchestrator/escalation.py`.

### F0 del canal WhatsApp Web no oficial (ADR-0074, propuesto)
- Spike `scripts/spike_neonize.py` + verificación de instalación.
- **Validado:** `neonize 0.5.2` instala e importa (con `_ctypes` disponible) y
  `NewAClient` instancia. **Hallazgo:** el Python fuente del venv
  (`/usr/local/Python-3.12.0`) se compiló **sin libffi** → `No module named
  '_ctypes'`; el connector necesita un Python con libffi (el del Docker sí lo tiene).
- **Pendiente manual** (requiere número de repuesto): QR, persistencia tras
  reinicio, supervivencia a recrear contenedor, media y reconexión tras caída de red.

### F1 (inicio) — sesión persistente del canal `waweb`
- Migración core **`0039_waweb_sessions`**: tabla `waweb_sessions` (`tenant_id` +
  RLS + FORCE + índice compuesto + único parcial por conexión + `deleted_at`) y
  modelo `WaWebSession` en `core/db/models.py`. Aplicar/revertir validado contra
  el stack local.
- **Tipo de conexión `whatsapp_waweb`** en el catálogo (`connection_types.py`,
  wizard `/admin/conexiones/waweb`): aparece en "Agregar conexión" con su aviso.
- **Política anti-baneo por tenant** (`core/waweb/policy.py`): modo solo-respuesta
  (default), delays y tope diario configurables en `behavior_config`, con clamps.
- **Paquete `packages/channel-waweb`** (entry_point `agent_core.channels` →
  provider `waweb`) con `WaWebInbound`/`WaWebOutbound`; el envío cruza por Redis
  request/reply (`core/waweb/handoff.py`) hacia el futuro proceso `connector`.
- **Vista `/admin/conexiones/waweb`**: aviso de riesgo con aceptación registrada
  (`risk_ack_at/by/version`) y QR por polling (`/qr.json`), a la espera del
  connector.
- **Runtime del connector (handlers reusables)**: `core/waweb/inbound.py`
  (publica el entrante al stream `agent_core:inbound` con dedupe por wamid) y
  `core/waweb/outbound.py` (drena `waweb:outbox` con delay anti-baneo y responde
  el wamid/error). Backend abstracto: el socket Neonize se enchufa después del
  spike.
- **Connector completo (endurecido)**: `store.py` (sesión efímera ↔ blob cifrado
  en `waweb_sessions.creds_encrypted`), `backend.py` + `backend_neonize.py`,
  `manager.py` (FSM, reconexión con backoff + *attempt guard*, persistencia,
  alertas), `registry.py`, `limits.py` (tope diario real + kill-switch) y
  `connector.py` (entrypoint). Rol `connector`: `scripts/entrypoint-connector.sh`
  + servicio `waweb-connector` (perfil `waweb`, tmpfs, **apagado por defecto**).
- **Config**: `WAWEB_ENABLED` (default **false**), `WAWEB_SESSION_DIR`,
  `WAWEB_SWEEP_INTERVAL_S`, `WAWEB_RECONNECT_MAX_S`; extra `waweb` (neonize) en
  `pyproject`. Imagen con `libmagic1`/`ffmpeg` (Neonize).
- **Coexistence (Stream B)**: badge "coexistencia" en `/admin/conexiones` y
  lectura de `is_on_biz_app` en el wizard (sin migración).
- Tests: `tests/test_waweb_{policy,handoff,connector,store,limits,manager}.py`.
- **Verificado en vivo (sin SIM):** el connector arranca, el barrido encuentra la
  sesión con riesgo aceptado, Neonize conecta y **genera un QR real** que se
  publica a Redis y el panel lo muestra (`/admin/conexiones/waweb`). Mapeo de
  eventos de Neonize confirmado en código (`event.qr`, `ConnectedEv`,
  `DisconnectedEv`, `LoggedOutEv`, `TemporaryBanEv`, `ConnectFailureEv`,
  `MessageEv`).
- **Fix de aislamiento (importante):** el connector corre como `app_runtime`
  (RLS); `active_sessions`/`set_status` y el store de sesión ahora iteran
  `tenants` y abren `tenant_scope` por tenant (antes RLS los dejaba en cero).
- **Arreglos tras la primera prueba en vivo:**
  - Remitente: `MessageSource.Sender` es un **proto JID** (no texto); se resuelve
    con `.User` y fallback LID→`SenderAlt`. Antes producía `whatsapp:+User` y las
    respuestas eran inentregables.
  - UI: se elimina el **reload en loop** al conectar; el polling se detiene y se
    muestra "Número vinculado" en el lugar. `logged_out` muestra un aviso claro.
  - Al `logged_out` se **vacía el outbox** del tenant (no más envíos fantasma).
  - `request_send` **falla rápido** si la sesión no está conectada (marca
    `waweb:ready:<tenant>` la fija el connector al conectar).
  - Detección de **número duplicado** entre tenants (alerta + se detiene el de
    mayor id).

## Sin versión — conexiones clasificadas + pack-calendar v2 (2026-09-26)

Conexiones del panel clasificadas por categoría (comunicación, correo, calendario)
y **pack-calendar v2**, con gating por conexión. Ver ADR-0069/0070/0072. Épica #97.

### Core
- **Catálogo de tipos de conexión** `connection_types` (tabla de sistema, patrón
  `plans`) + `sync_connection_types()` en el arranque (ADR-0069).
- `channel_connections` gana `type_id`/`category`; `save_connection` se generaliza
  (valida credenciales contra el `credentials_schema` del tipo), corrige el filtro
  de tenant del lookup y acota la primacía a la categoría de mensajería.
- `/admin/conexiones`: el modal "Agregar conexión" se genera desde el catálogo,
  agrupado por categoría; los tipos `manual` (correo SMTP) abren un formulario
  genérico de credenciales (FormEngine + SecretBox).
- **Gating de tools por conexión activa** (`MCPTool.requires_connection_types`,
  ADR-0072): en pre-prompt y en `_tool_denial`.

### pack-calendar v2
- Los tokens OAuth de Google pasan a la **conexión del tenant** (vault), con
  dual-read transitorio de `plugin_configs` (ADR-0070).
- **Submenú** `/calendar/*` (ADR-0061/0063): Conexión · Horarios y calendario ·
  Tipos de cita · Citas.
- Tablas `pack_calendar_appointment_types` y `pack_calendar_appointments`
  (`tenant_id` + RLS + FORCE RLS).
- `create_calendar_event` espeja la cita en la tabla; `check_availability` usa el
  horario de atención configurado.
- Sin cuenta conectada, el asistente **no** ofrece agendar (gating).

### Correo saliente (ADR-0071)
- Adaptador de canal `smtp` (built-in, `smtplib`) reutilizando la cola de salida
  (`outbound_send`): un envío es un `Message` del canal `email`.
- Tool **`send_email`** (gated por conexión `email_smtp`): crea el `Customer` +
  `Conversation` del destinatario y encola el correo. Sin inbound.

### Migraciones
- Core `0038` (catálogo de conexiones). Pack-calendar `0001` (tipos de cita y citas).

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
- **Bloque E (cobros al propietario, Épica 9)**: pantalla **Cobros** (generar el
  cobro del periodo, registrar pago, crear link de pago). Tablas
  `cobros_propietario` y `links_pago`.
- **Bloque F (RBAC por sub-sección, ADR-0063)**: los items de `admin_menu` declaran
  `section_key`; el RBAC resuelve por sub-sección (`pack_cobranza:carteras`,
  `:clientes`, `:cobros`, …), así un colaborador puede tener write en unas y read
  en otras vía grants.
- **Bloque D (API de campo, ADR-0057)**: `public_routes()` en `BaseModule` + API
  pública `/api/pack/pack_cobranza` con **JWT propio del pack** (HS256, sin deps).
  Endpoints: login de dispositivo, mis-carteras, clientes-del-día y **sync offline
  idempotente** (visita/pago/abono). Tablas `devices`, `intentos_acceso`,
  `sync_eventos` + `gestores.password_hash`. Pantalla **Dispositivos** (genera
  código + API key, ver intentos de acceso).
- **Reportes globales**: consolidado multi-cartera (clientes, préstamos, saldo,
  cobrado, caja, gastos, cuotas vencidas) + liquidaciones recientes.
- **Alertas de acceso** (sección Seguridad): intentos de acceso de la app de campo
  con los denegados resaltados.
- **Editar cuota**: formulario completo (valor, vencimiento, estatus) con auditoría.
- **Conversación del cliente**: acceso directo desde el detalle del cliente a la
  conversación del core (si el cliente está enlazado a un `Customer`).
- **Auditoría**: pantalla de `AuditoriaCartera` (imborrable) con filtros por entidad,
  operación y rango de fechas.
- **Promesas**: listado filtrable por estado + marcar cumplida/incumplida (con auditoría).
- **Caja** (pestaña de la cartera): saldo inicial/actual + libro de movimientos
  (pagos, abonos, inyecciones, gastos aprobados, préstamos y ajustes) + **ajuste manual**
  con motivo. Ahora **inyecciones y gastos aprobados mueven el saldo de caja**, para que
  el libro cuadre.
- **Eventos de sincronización** de la app de campo en la pantalla **Dispositivos**
  (`SyncEvento`, con los `error` resaltados).
- **Evidencias de cliente** (Bloque H): endpoint de la API de campo
  `POST /api/pack/pack_cobranza/clientes/{id}/evidencia` (multipart, foto facial o
  documento; jpeg/png/webp/pdf, máx. 5 MB) que guarda en
  `TENANTS_ROOT/<slug>/evidencias/` (fuera de Postgres) + galería en el detalle del
  cliente con servido tenant-scoped.
- **Cierre diario** (Bloque H): cron `cierre_diario` que congela `ReporteDiario` y
  `CarteraEstadisticasSnapshot` por cartera (idempotente por fecha), con botón
  "Generar cierre del día" e histórico en la pestaña **Estadísticas**.
- **Optimización de trayecto** (Bloque H): orden por vecino más cercano (haversine)
  desde la apertura del día → `CarteraOptimizadaLog` + plan y ruta en el mapa.
- **Comunicación con el propietario** (Épica 9, HU-63; ADR-0064): hilo por tenant
  (`conversaciones_propietario` + `mensajes_propietario`) con notificaciones de cobro
  auto-registradas, mensajes manuales saliente/entrante y enlace **wa.me** al
  WhatsApp del propietario (`CobranzaConfig.propietario_whatsapp`).
- **API de campo del gestor — contrato de la APK** (ADR-0065): router
  `api_gestor.py` en la API pública, con el contrato exacto de `solvershub-apk`
  (camelCase, IDs string). **F1**: `POST /auth/gestor/login` y
  `/auth/propietario/login` (`tenant`+`usuario`+`password` → access+refresh),
  `POST /auth/refresh`, `PATCH /perfil`.
- **Autovinculación de dispositivo (HU-39/43, ADR-0068)**: `POST/GET /gestor/dispositivo`
  (IMEI + WhatsApp, estado `pendiente`); el login exige coincidir con un device
  `activo` con IMEI (403 + `IntentoAcceso` si no) y no bloquea el primer vínculo;
  el panel **Dispositivos** aprueba/bloquea. Migración `0012` (IMEI único). X25519/HU-40 diferido.
- **Identidad unificada: el gestor es un colaborador del core** (ADR-0066). El
  propietario es `Admin` + `TenantUser(owner)`; el gestor es `Admin` +
  `TenantUser(colaborador)` **con perfil `Gestor`** (`admin_id`, sin credenciales
  propias). El core gana el punto de extensión `BaseModule.collaborator_roles` +
  `on_collaborator_role` (sin tabla nueva): `/admin/colaboradores` ofrece "Gestor
  de cobranza" y, al crearlo, el pack crea el perfil; crear un gestor desde
  Gestores crea la cuenta (aparece en Colaboradores). Migración `0009`.
- **API de campo del gestor — F2 (lecturas)**: mis-carteras (con `config` y
  `permisos`), notificaciones, trayecto-diario, estadísticas (con snapshot para
  `anterior`/`deltas`), clientes, tarjeta, préstamos, estado-cuenta, detalle de
  cuota, notas, liquidaciones y servido de evidencias por Bearer. Todo
  tenant-scoped y filtrado por cartera visible (gestor → las suyas; propietario →
  todas); `pack_cobranza/estadisticas.py` comparte los conteos con el cron diario.
- **API de campo del gestor — F3 (escrituras)**: visitas (pago/no-pago con pago o
  abono y promesa), gastos (multipart con evidencias), alta/edición de cliente
  (multipart con evidencias), préstamos, edición/eliminación de cuotas y bajas de
  pago/abono (con contraseña del admin + auditoría), notas, apertura/posición,
  trayectoria real, generar trayecto, editar cartera/configuración, liquidaciones y
  export imprimible (HTML en v1).
- **Gestor B (REQ-APK-002/003)**: `trayecto-diario` incluye `latitud`/`longitud` por
  cliente (cliente cercano en la app); la liquidación calcula KPIs reales
  (`estimado_acobrar`, `total_inyeccion`, `suma_cartera`, `comision_porcentaje`)
  y los conteos del día (`pagaron`/`faltan`/`totalDia`/`sinCuentas`/horas) —
  migración `0010`.
- **Capacidad de pagos del core + cobro digital (ADR-0067)**: registry
  `core/payments/` (molde `LeadRegistry`), topics `ON_PAYMENT_*` movidos al core y
  `qr_png_data_uri`. `pack_payments` registra su capacidad (Wompi) y `pack_cobranza`
  crea el link/QR de una cuota (`POST …/cuotas/{id}/link-pago`), registra el `Pago`
  al confirmarse por EventBus (idempotente), o devuelve la **cuenta informativa**
  de `CobranzaConfig` si no hay pasarela. Migración `0011` (LinkPago→cuota).
- **API de campo del gestor — F4 (offline)**: `POST /sync-offline/eventos`
  (idempotente por `eventoIdCliente`, **ack por evento**, savepoint por evento;
  tipos `visita`/`pago`/`abono`/`gasto` con evidencias base64/`trayectoria`/
  `cambio_cliente`) y `GET /sync-offline/trayecto-diario`.
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
- **ADR-0063**: RBAC por sub-sección de pack (`section_key` en `admin_menu`).
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