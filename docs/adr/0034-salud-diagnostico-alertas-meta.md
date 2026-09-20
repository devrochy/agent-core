# ADR-0034 — Health checks, diagnóstico y alertas de conexiones Meta

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

El esquema ya anticipaba este ticket desde una tanda anterior:
`MetaConnection.health` (JSONB), `status_reason` y `last_health_check_at`
existían en el modelo pero nadie los escribía (salvo `status_reason`, ya
usado por `meta_subscriptions_health.py`). El ticket pide 7 checks, un
cron horario + uno de 15 min ya existente, alertas reales (Slack/Sentry,
ninguno de los dos existía en el repo hasta ahora), un dashboard de
tarjetas con semáforo, reparación inline por check, y una ruta de escape
manual para cuando Embedded Signup falla en self-hosted.

## Decisión

1. **Un solo tipo de resultado de check**: se extendió `PreflightCheck`
   (ya existente en `meta_preflight.py`) con `checked_at`/`repair_action`
   en vez de crear un segundo tipo paralelo.
2. **`app_subscription_active` (el check más barato/mayor impacto según el
   ticket) NO se repite**: `meta_health.py::check_app_subscription` lee el
   estado ya persistido por el cron de 15 min existente
   (`meta_subscriptions_health.py`), a través de la API pública
   `meta_subscriptions.list_app_subscriptions` -- nunca importa el modelo
   `MetaAppSubscription` directo (respeta el aislamiento por módulo dueño
   ya establecido y verificado por
   `tests/test_meta_app_subscriptions_solo_desde_su_modulo.py`).
3. **`token_valid` (`debug_token`) va con `httpx` directo**, no
   `graph_fetch`: el `access_token` de esa llamada es el de LA APP
   (`app_id|app_secret`), no el token que se inspecciona -- no calza con
   el cliente único, que asume Bearer+appsecret_proof sobre el MISMO token
   que envía.
4. **Cambio de comportamiento explícito del ticket, aplicado sin
   preguntar**: `meta_usage.py::PAUSE_THRESHOLD_PCT` baja de 90 a 80
   ("worst_pct de cuota >= 80 -> pausar meta:outbound"), convergiendo con
   `WARN_THRESHOLD_PCT` (ya era 80).
5. **Alertas: módulo mínimo nuevo (`core/shared/alerts.py`)**, no una
   integración pesada. Slack vía webhook URL configurable (best-effort,
   nunca bloquea); Sentry OPCIONAL, mismo patrón que OTel
   (`settings.sentry_enabled`, no-op sin DSN), con `before_send` de
   redacción de tokens de Meta como red de ÚLTIMA instancia (el código ya
   no debería mandarle un token a Sentry por construcción).
6. **Fix real encontrado al implementar el check de "worker caído"**:
   `WebhookEvent.processed_at` NUNCA se escribía en ningún lado -- el
   check literal del ticket ("processed_at null y received_at < now()-5min")
   hubiera disparado SIEMPRE, para cada fila, sin señal real. Se agregó
   `webhooks.py::_mark_entry_processed`, que marca `processed_at` después
   de que los tres dispatch de un entry terminan sin excepción -- ahora el
   check mide algo real (retraso de procesamiento), no ausencia total de
   escritura.
7. **`record_invalid_signature` se indexa SOLO por `meta_app_id`, sin
   tenant_id** -- decisión deliberada: la firma inválida es un ataque
   sobre el App Secret COMPARTIDO (BYOK: varios tenants pueden compartir
   una app), no sobre un tenant puntual. Alertar por tenant fragmentaría
   la señal justo cuando más importa verla agregada.
8. **Ruta de escape manual**: crea `MetaConnection`/`WabaAccount`/
   `PhoneNumber` con `tenant_id` SIEMPRE de la sesión
   (`get_current_tenant_id()`), nunca de un campo del formulario -- y
   valida el token contra `debug_token` ANTES de persistir nada, para no
   guardar un token roto a ciegas.

## Hallazgo del auditor-multitenant (2026-09-20) corregido

Severidad baja: `meta_manual_connection_submit` validaba el token siempre
contra la app compartida de `.env` (`settings.meta_app_id`/`meta_app_secret`),
sin considerar que un tenant BYOK tiene su propia app configurada -- se
corrigió para resolver credenciales por tenant vía
`meta_preflight.resolve_app_credentials` primero, cayendo a la app global
solo si el tenant no tiene una propia.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Repetir `GET /{app_id}/subscriptions` en el cron horario | Ya lo hace el cron de 15 min; duplicar el Graph call no agrega información y gasta rate limit. |
| Integración completa de Sentry (tracing, releases, etc.) | Fuera de alcance -- el ticket solo pide `before_send` de redacción; se limita a `sentry_sdk.init` + hook, mismo criterio de "no fork, no over-engineering". |
| Alertar por tenant en `record_invalid_signature` | La app se comparte entre tenants (BYOK); fragmentar el contador por tenant diluye la señal de "el App Secret puede estar comprometido/rotado", que es a nivel de app. |

## Consecuencias

**Positivas:** el semáforo del panel refleja 7 señales reales con
reparación de un click cada una; las alertas ya no dependen de que alguien
esté mirando logs; el check de "worker caído" ahora es confiable.

**Negativas / lo que aceptamos pagar:** una dependencia nueva
(`sentry-sdk`), inerte a menos que se configure `SENTRY_DSN`.

**Qué haría que revisáramos esto:** si `PAUSE_THRESHOLD_PCT` a 80% resulta
demasiado agresivo en producción (pausas frecuentes con margen todavía
disponible), reconsiderar el número -- el ticket lo fija en 80 de forma
explícita, pero es el tipo de umbral que conviene validar con tráfico real.

## Impacto en el código

- `core/gateway/meta_health.py`, `core/gateway/meta_preflight.py`,
  `core/gateway/webhook_verify.py`, `core/gateway/webhooks.py`,
  `core/gateway/meta_usage.py`, `core/gateway/meta_template_webhooks.py`.
- `core/shared/alerts.py`, `core/shared/redact.py`,
  `core/observability/sentry_setup.py`.
- `core/tasks/meta_health_check.py`, `core/tasks/celery_app.py`.
- `core/admin/views.py`, `core/admin/templates/meta_salud.html`,
  `core/admin/templates/meta_manual.html`.
- `core/config.py` (settings de Slack/Sentry).
