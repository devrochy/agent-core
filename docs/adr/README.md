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
| 0055 | Intake público de leads desde la landing | propuesto |
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
