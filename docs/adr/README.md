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
| 0023 | pack-payments: coordinación entre packs vía eventos, sin acoplarlos | aceptado |
| 0024 | Salud de plugin por tenant (`plugin_tenant_state`), no `auto_disable` global | aceptado |
| 0025 | `completion_claims` custom por tenant: frases literales, nunca regex crudo | aceptado |
| 0026 | Playbooks: override de contenido por tenant vía filesystem | aceptado |
| 0027 | Cifrado de secretos en BD: AES-256-GCM con rotación de claves | aceptado |
| 0028 | `webhook_events` como tabla de sistema (excepción a invariante nº1) | aceptado |
| 0029 | `meta_app_subscriptions` como tabla de sistema (excepción a invariante nº1) | aceptado |
| 0030 | `connection_wizards`: máquina de estados reanudable del Embedded Signup | aceptado |
| 0031 | Ciclo de vida de plantillas de WhatsApp: máquina de estados por webhook y resolución de tenant sin RLS | aceptado |
| 0032 | Cola de salida vía Celery, idempotencia por `client_message_id` y máquina de estados de mensajes | aceptado |
| 0033 | Contabilidad de costes de Meta: dos tablas, dos fuentes de verdad, nunca una tarifa local | aceptado |
| 0034 | Health checks, diagnóstico y alertas de conexiones Meta | aceptado |
| 0035 | Checklist de promoción a producción: 9 checks automáticos reales + manuales persistidos | aceptado |
| 0036 | Wizard BYOK (app propia) para modo prueba de WhatsApp | aceptado |
| 0037 | `verify_token` propio por conexión BYOK en el handshake del webhook | aceptado |
| 0038 | Activar/desactivar/eliminar conexiones de Meta (soft-delete) | aceptado |
| 0039 | Enumeración cross-tenant de assets de Meta para crons (SECURITY DEFINER) | aceptado |
| 0040 | Resolución de credenciales de webhook por tenant (manual → BYOK → `.env`) | aceptado |
| 0041 | Asistente de token del wizard BYOK (extender/verificar) y expiración | aceptado |
| 0042 | Worker Celery: `include` de tasks + puente async con loop persistente | aceptado |
| 0043 | Alta de tenant conecta WhatsApp por el flujo BYOK | aceptado |
| 0044 | Registro de crons de packs (import de `jobs` + `beat_init`) | aceptado |
| 0045 | Planes y cuotas por tenant: catálogo versionado, medición derivada y bloqueo duro | aceptado |
| 0046 | Cobro de la suscripción de plataforma vía Wompi (link por período) | aceptado |
| 0047 | Autorización del panel por permisos de sección (RBAC configurable) | aceptado |
| 0048 | Administrador por tenant, colaboradores y alta con credenciales | aceptado |
| 0049 | Configuración del agente por pestañas y pagos BYOK en el pack | aceptado |
| 0050 | AgentSpec v2.0.0: contrato que no miente y agente configurable | aceptado |
| 0051 | Operación y gobierno del agente (rollback, horarios, guardrails declarativos, evals) | aceptado |
| 0052 | Extensibilidad de capacidades: tools declarativas y canales por entry_points | aceptado |
| 0053 | Memoria: retención, purga por cliente y redacción de PII | aceptado |
| 0054 | Flujo de atención declarativo por pack, personalizable por tenant | aceptado |
| 0055 | Intake público de leads desde la landing | reemplazado por 0057 |
| 0056 | Leads globales con contribuciones por pack (`LeadProvider`) | aceptado |
| 0057 | Rutas públicas de pack e identidad de pack (auth de usuarios finales) | aceptado |
| 0058 | PostGIS en el despliegue del core | aceptado |
| 0059 | Aislamiento multi-tenant del pack de cobranza (1 tenant = 1 propietario) | aceptado |
| 0060 | Alineación del pack-cobranza al modelo de dominio de app-cobranza (Propietario/Gestor/Cartera) | aceptado |
| 0061 | `admin_menu` de packs por secciones y visibilidad por rol | aceptado |
| 0062 | El propietario es el tenant: el pack no duplica la identidad | aceptado |
| 0063 | RBAC por sub-sección de pack | aceptado |
| 0064 | Comunicación con el propietario: bitácora por tenant con enlace wa.me | aceptado |
| 0065 | API de campo del gestor: el pack implementa el contrato de la APK | aceptado |
| 0066 | El gestor es un colaborador del core: identidad unificada | aceptado |
| 0067 | Capacidad de pagos del core y cobro digital en cobranza | aceptado |
| 0068 | Autovinculación de dispositivo del gestor (HU-39/43) | aceptado |
| 0069 | Catálogo de tipos de conexión y generalización de `channel_connections` | aceptado |
| 0070 | Vault unificado: los tokens OAuth de Google pasan a la conexión del tenant | aceptado |
| 0071 | Canal de correo saliente (sin inbound) | aceptado |
| 0072 | Gating declarativo de tools por prerequisitos de conexión | aceptado |
| 0073 | Panel y URLs de la app bajo un prefijo de ruta del proxy | aceptado |
| 0074 | Canal WhatsApp Web no oficial (`waweb`) vía Neonize, coexistente con Meta | propuesto |
| 0075 | Playground "Probar asistente": conversación de prueba sin canal | aceptado |
| 0076 | Panel API-first y frontend separado (reemplaza la decisión operativa de `docs/ADR/ADR-007`) | aceptado |
| 0077 | API unificada `/api/v1` y realms de autenticación (modifica 0057 y 0065) | aceptado |
| 0078 | El wizard de WhatsApp (Embedded Signup/BYOK) permanece en el panel clásico | aceptado |
| 0079 | API admin de packs bajo `/api/v1/pack/<id>` (punto de extensión) | aceptado |
| [0080](0080-wizard-whatsapp-en-panel-nuevo.md) | El wizard de WhatsApp se reconstruye en el panel nuevo (supera el 0078) |
| [0081](0081-login-unificado-e-invitaciones.md) | Identidad unificada: login sin slug, selección de negocio y membresía por invitación | aceptado |
| [0082](0082-suscripcion-recurrente-de-cartera.md) | Suscripción recurrente de la cartera (Opción 2, base) | aceptado |
| [0083](0083-pack-subscriptions-opcional.md) | `pack_subscriptions`: suscripción de cliente final sobre planes (Opción 1, opcional) | aceptado |
| [0084](0084-calendario-interno-pack-calendar.md) | Calendario interno del pack-calendar (sin Google) | aceptado |
| [0085](0085-persistencia-artefactos-tenant.md) | Persistencia de los artefactos de tenant en despliegue | aceptado |
| [0086](0086-modelos-deepseek-vigentes.md) | Modelos DeepSeek vigentes (V4): `deepseek-flash` con thinking explícito (supersede parcial de ADR-003) | aceptado |
| [0087](0087-camino-del-dinero-en-comercio.md) | Camino del dinero en comercio: total del servidor, link versionado y webhook validado (HU-COM-01) | aceptado |
| [0088](0088-error-de-negocio-no-apaga-packs.md) | Un error de negocio no cuenta como falla de salud (enmienda ADR-0024, HU-ARCH-18) | aceptado |
| [0089](0089-plugin-del-tenant.md) | Plugin del tenant: nivel 3 de personalización, acotado a un negocio (HU-FWK-13) | aceptado |
| [0090](0090-reclamo-pendientes-claim-con-estado.md) | Reclamo automático de pendientes y claim de idempotencia con estado (HU-ARCH-06) | aceptado |
| [0091](0091-fecha-actual-y-zona-horaria-del-negocio.md) | Fecha actual en el prompt y zona horaria del negocio (HU-CAL-04) | aceptado |
| [0092](0092-fallo-de-guardrail-neutral-configurable.md) | Fallo de guardrail con respuesta neutral configurable (HU-CAL-04) | aceptado |
| [0093](0093-callback-oauth-google-en-el-panel.md) | El retorno OAuth de Google Calendar entra por el panel (supersede parcial de ADR-0070, HU-CAL-07) | aceptado |
| [0094](0094-credenciales-de-meta-por-conexion.md) | Credenciales de Meta por conexión, con el origen guardado (`byok` / `embedded_signup` / `manual`; supersede parcial de ADR-0040 §3 y ADR-0041 §5, enmienda ADR-0034, HU-CONN-01) | aceptado |
| [0095](0095-numero-y-waba-de-un-solo-negocio.md) | Un número o una WABA de Meta pertenecen a un solo negocio: ruteo por número primero, resolvedores sin ambigüedad y altas verificadas contra Graph (enmienda ADR-0012) | aceptado |
| [0096](0096-busqueda-global-app.md) | Búsqueda global de la app oficial: `GET /api/v1/search` con providers que registran los packs, filtrados por acceso de lectura a su sección (HU-APP-14) | aceptado |
| [0097](0097-sync-incremental-y-auditoria-movil.md) | Sync incremental por entidad (`GET /api/v1/sync/{entidad}`, adapters de los packs) y auditoría móvil idempotente en `mobile_audit_events` (HU-APP-15) | aceptado |
| [0098](0098-recuperacion-y-token-de-gestor.md) | Recuperación de contraseña con código de un solo uso por SMTP de sistema y token de gestor desde la sesión admin (HU-APP-02; antes numerado 0094) | aceptado |
| [0099](0099-notificaciones-push-expo.md) | Notificaciones push de la app oficial vía Expo Push Service: `push_tokens` por negocio y envío por Celery (HU-APP-05; antes numerado 0095) | aceptado |
| [0100](0100-evento-del-webhook-solo-actua-sobre-un-negocio-autorizado.md) | Un evento del webhook solo actúa sobre un negocio autorizado por su firma: firma por grupo en el genérico, y plantillas y estados atados al negocio de la entry (enmienda ADR-0040, ADR-0031 y ADR-0032) | aceptado |
| [0101](0101-byok-degradado-conserva-su-secreto.md) | Una conexión BYOK degradada o con error sigue prestando su secreto y su verify token; `revoked` y `draft` no (enmienda ADR-0040) | aceptado |
| [0102](0102-conexion-meta-se-recupera-sola.md) | Una conexión de Meta en `error`/`degraded` se recupera sola, y solo mejora de estado con el diagnóstico verificado (token, y en BYOK su app suscrita); una BYOK sin credenciales propias pasa a `error`; alertas de los crons una vez al día (enmienda ADR-0101 y ADR-0029) | aceptado |
| [0103](0103-politica-antiban-de-waweb-no-escala.md) | El rechazo por política anti-baneo de `waweb` (tope diario o pausa) no escala ni pausa el bot: difiere el mensaje hasta que el envío vuelva a estar permitido (enmienda ADR-0074, issue #231) | aceptado |
| [0104](0104-autoescalado-ignora-cortesias-y-preguntas.md) | El autoescalado por turnos sin resolver ignora cortesías y preguntas de avance (enmienda ADR-0051, issue #234) | aceptado |
| [0105](0105-bloqueo-progresivo-de-login.md) | Bloqueo progresivo de inicio de sesión: 5 fallos del mismo identificador o IP en 10 min → 15 min de bloqueo con mensaje neutro, en la API admin y en la API de campo con aislamiento por negocio (HU-ARCH-04) | aceptado |
| [0106](0106-suscripciones-v1.md) | Suscripciones v1: contrato único de 8 operaciones en `core/subscriptions`, selección de proveedor por tenant con error de ambigüedad y tools `sub_*` en el core que despachan al proveedor (supersede/enmienda ADR-0082 y ADR-0083; HU-SUB-01) | aceptado |
| [0107](0107-eventos-de-dominio-durables.md) | Eventos de dominio durables con outbox: tabla `domain_events` con RLS, escritura en la misma transacción del cambio, despachador Celery con backoff y DLQ, y suscripciones con tracking del host (enmienda ADR-0067; HU-SUB-03) | aceptado |
| [0108](0108-plantillas-por-evento.md) | Plantillas de WhatsApp por evento: mapa evento → plantilla aprobada por negocio, texto de respaldo dentro de la ventana, `sin_plantilla` visible, variables solo de rutas permitidas del evento y 409 real para el operador (HU-ARCH-13) | aceptado |
| [0109](0109-composicion-de-flujos-multi-pack.md) | Composición de flujos multi-pack: core + todos los packs habilitados + secciones `tenant`; IDs con namespace, tools del core siempre disponibles y el `flow.json` del negocio deja de ser autoritativo (enmienda ADR-0054; HU-FWK-01) | aceptado |
| [0110](0110-presets-de-agente.md) | Presets de agente: manifiesto corto en `core/presets/` que referencia packs + fragmentos por pack y vertical (config sin secretos y `seed`); `apply_preset` idempotente, packs con claves quedan pendientes sin romper el alta (HU-FWK-02) | aceptado |
| [0111](0111-cambio-de-plan-y-catalogo-de-planes.md) | Cambio de plan con precio del servidor: catálogo de planes como capacidad del core (commerce lo registra), `sub_cambiar_plan` solo con `plan_code`, upgrade con cargo prorrateado, downgrade programado, `permite_varias` opcional y referencia del pedido (enmienda ADR-0106; HU-SUB-07) | aceptado |
