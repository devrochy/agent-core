# ADR-0029 — `meta_app_subscriptions` como tabla de sistema (excepción a invariante nº1)

- **Estado:** aceptado
- **Fecha:** 2026-09-21

## Contexto

Se agrega el registro y verificación de suscripciones a nivel de **app** de
Meta (`POST /{app_id}/subscriptions`: `callback_url`, `verify_token`,
`fields` por objeto). Esto es distinto de lo que ya existe:

- `webhook_events` (ADR-0028) es un log por **entrega** de webhook.
- `WabaAccount.subscribed` / `FbPage.subscribed_fields` son suscripciones por
  **objeto individual** (`POST /{waba_id}/subscribed_apps`).

Una suscripción de app aplica a la **app entera**, no a un tenant. En este
repo (multi-tenant, BYOK real vía `MetaConnection.meta_app_id`), una misma
app de Meta puede estar en uso por varios tenants al mismo tiempo — la
invariante nº1 de `CLAUDE.md` (`tenant_id uuid not null` + RLS) no aplica
porque el recurso mismo no pertenece a un tenant: pertenece a la app.

## Decisión

`meta_app_subscriptions` es una tabla de **sistema** (misma categoría que
`admins`/`plugins`/`webhook_events`): sin RLS, y sin siquiera columna
`tenant_id` (a diferencia de `webhook_events`, que sí la tiene nullable
porque cada evento SÍ pertenece potencialmente a un tenant una vez resuelto
— acá no hay tenant al que resolver, el dueño es el `meta_app_id`). Clave
única `(meta_app_id, object)`.

Todo acceso de escritura pasa por `core/gateway/meta_subscriptions.py`
(`upsert_app_subscription`/`delete_app_subscription`) y por el cron de salud
(`core/tasks/meta_subscriptions_health.py`) — verificado por
`tests/test_meta_app_subscriptions_solo_desde_su_modulo.py`, mismo patrón
que `tests/test_webhook_events_solo_desde_webhooks.py`. Toda lectura desde
el panel admin (`core/admin/views.py`) filtra por los `meta_app_id` que
resuelven las conexiones del tenant activo — nunca expone filas de otra app
que el tenant no usa, aunque la tabla en sí no tenga RLS que lo garantice.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar el estado dentro de `MetaConnection` (una columna JSON por conexión) | Una misma app puede estar detrás de varias `MetaConnection` (una por tenant que la usa); duplicaría el mismo estado N veces y podría desincronizarse entre copias. |
| No persistir nada, siempre pedir a Meta en vivo (`GET /{app_id}/subscriptions`) en cada carga de panel | Cada carga de página gastaría una llamada de Graph API por app mostrada, compitiendo con el usage/rate-limit real de la app (ver `meta_usage.py`) solo para pintar una tabla de estado — el cron de 15 min ya mantiene esto fresco sin ese costo. |

**Actualización 2026-09-21 (auditor-multitenant)**: el cron de salud resuelve
el `app_secret` de la app usando la conexión más antigua entre los tenants
que la comparten (BYOK), asumiendo que todos guardaron el mismo secreto para
la misma app. Si divergen, se loguea una advertencia
(`meta_subscriptions_health_app_secret_divergente`) en vez de fallar en
silencio -- no es una fuga de datos (el secreto nunca se expone), pero sí una
inconsistencia operativa que merece visibilidad.

## Consecuencias

**Positivas:** una sola fuente de verdad por `(meta_app_id, object)`,
compartida correctamente entre los tenants que usan la misma app; el cron de
salud puede marcar `MetaConnection.status = 'degraded'` con datos frescos
sin flood de llamadas a Graph API por carga de página.

**Negativas / lo que aceptamos pagar:** sin RLS, un futuro
`select(MetaAppSubscription)` sin acotar por los `meta_app_id` del tenant
activo mezclaría el estado de apps de otros tenants — no hay secretos en esta
tabla (ni `app_secret` ni `verify_token`, solo `callback_url`/`active`/
`fields`), pero sigue siendo información de configuración de otro cliente
que no debería filtrarse. **Qué haría que revisáramos esto de nuevo:** si se
agrega un endpoint que liste `meta_app_subscriptions` sin acotar por las
conexiones del tenant activo (ej. un panel de super-admin "todas las apps") —
en ese momento, decidir si migrar a un modelo con `tenant_id` (una fila por
tenant que usa la app, con posible duplicación) o mantener la agregación por
app con un chequeo de pertenencia explícito y testeado.

## Impacto en el código

`alembic/versions/0022_meta_app_subscriptions.py`, `core/db/models.py`
(`MetaAppSubscription`), `core/gateway/meta_subscriptions.py`
(`upsert_app_subscription`/`list_app_subscriptions`/`delete_app_subscription`,
único punto de escritura de código de aplicación), `core/tasks/
meta_subscriptions_health.py` (cron de salud, también escribe), `core/admin/
views.py` (rutas `/admin/conexiones/webhooks*`, lectura acotada por tenant),
`tests/test_meta_app_subscriptions_solo_desde_su_modulo.py`.
