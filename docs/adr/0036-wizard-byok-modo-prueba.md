# ADR-0036 — Wizard BYOK (app propia) para modo prueba de WhatsApp

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

El wizard de Embedded Signup (`core/gateway/meta_onboarding.py`) usa
SIEMPRE la app del framework (`settings.meta_app_id/meta_app_secret/
meta_config_id`) vía OAuth -- correcto para producción, donde el
framework actúa como Tech Provider/ISV sobre una app YA revisada del
cliente. Pero el número de prueba gratis de Meta vive DENTRO de la app
que el propio tenant crea en developers.facebook.com (pestaña "API
Setup" del producto WhatsApp) -- pedirle a alguien que solo quiere
probar el asistente que pase por el Login Configuration del framework
no tiene sentido.

`meta_preflight.resolve_app_credentials` ya sabía LEER credenciales
"propias" de un tenant (`MetaConnection.meta_app_id/meta_config_id` +
`meta_token_store` con `kind="app_access"`) desde que se introdujo, pero
nada las escribía -- era una lectura huérfana.

## Decisión

1. **"Modo prueba" en `/admin/conexiones/wizard/whatsapp/elegir-entorno`
   reemplaza Embedded Signup por un modal BYOK** de 4 pasos (crear app →
   App ID/Secret → número de prueba (`phone_number_id`+access token de
   la pestaña API Setup) → webhook). Modo producción sigue igual.
2. **`core/gateway/meta_byok.py::save_byok_connection`** verifica contra
   Graph API (reusa `verify_meta_credentials`, ya usado por el modal
   legado de `ChannelConnection`) ANTES de persistir -- nunca guarda a
   ciegas. Crea/actualiza `MetaConnection` (aprovecha el `UniqueConstraint
   (tenant_id, channel, environment, meta_app_id)` que ya admitía un
   `meta_app_id` propio del tenant) + `PhoneNumber` (ya viene registrado
   por Meta, no hace falta el paso de PIN de `register_number_step`).
3. **Nuevo `kind` de `meta_token_store`: `cloud_api_access_token`**
   (migración `0030`, agrega el valor al `CHECK` de
   `meta_private.meta_tokens.kind` -- mismo mecanismo que la migración
   `0025` agregó `phone_register_pin`). Deliberadamente distinto de
   `business_integration_system_user`, que es semánticamente el token
   que emite el flujo OAuth de Embedded Signup, no uno pegado a mano.
4. **No hace falta tocar `sender.py::resolve_credentials`**: la conexión
   BYOK es una `MetaConnection` más, resuelta por el mismo camino que ya
   usa Embedded Signup (`meta_connection_id` + `meta_token_store`) -- no
   es un tercer origen de credenciales de envío, así que no dispara la
   condición de revisión que marca ADR-0030 ("si se agrega un tercer
   origen... convertir el if/elif en resolución por prioridad").
5. **Sin pantalla de resumen nueva**: al guardar, redirige a
   `/admin/conexiones/salud` (ya existente, ticket anterior) -- esa
   página ya diagnostica cualquier `MetaConnection` sin importar cómo se
   creó.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reusar `ChannelConnection` (modal legado) en vez de `MetaConnection` | Es un universo aparte (cifrado Fernet, sin FK a `WabaAccount`/`PhoneNumber`, sin health checks ni promoción a producción) -- guardar ahí perdería toda la integración ya construida para `MetaConnection`. |
| Reusar el `kind` `business_integration_system_user` para el access token BYOK | Ese kind es semánticamente "token emitido por el OAuth de Embedded Signup" -- mezclarlo con un token pegado a mano por el admin confunde el origen de la credencial en auditorías futuras. |
| Wizard multi-request con `ConnectionWizard` (como Embedded Signup) | No hace falta persistir estado intermedio -- son 4 campos de texto en un solo POST síncrono, no un flujo asíncrono con popup de OAuth. |

## Hallazgo del auditor-multitenant corregido

Sin hallazgos de severidad alta. Dos hallazgos MEDIOS, ambos corregidos:

1. La query de `PhoneNumber` dentro de `save_byok_connection` filtraba
   solo por `connection_id`+`phone_number_id`, sin `tenant_id` explícito
   -- hoy no era una fuga real (`connection_id` ya nace de una fila
   filtrada por tenant), pero rompía la invariante de defensa-en-
   profundidad que sí siguen `meta_token_store.get_token`/`revoke_token`.
   Corregido agregando `PhoneNumber.tenant_id == tenant_id` al filtro.
2. Estado parcial posible: si `put_token` fallaba DESPUÉS de verificar
   credenciales válidas contra Graph (fallo de infra, no de
   credenciales), la conexión podía quedar `status="connected"` sin uno
   o los dos secretos reales. Corregido: la conexión queda en `"pending"`
   mientras se guardan los tokens, y recién se marca `"connected"`
   después de que AMBOS `put_token` tuvieron éxito; si alguno falla,
   queda en `"error"` con `status_reason` explicando el motivo (test de
   regresión: `test_fallo_guardando_token_marca_error_y_no_deja_connected`).

## Consecuencias

**Positivas:** cierra el hueco entre `resolve_app_credentials` (lectura)
y la falta de escritura BYOK; modo prueba ya no depende de tener
configurado `META_APP_ID/META_APP_SECRET/META_CONFIG_ID` del framework;
un fallo de infraestructura al guardar los tokens ya no deja una
conexión "connected" engañosa -- queda en "error", visible en
`/admin/conexiones/salud`.

**Negativas / lo que aceptamos pagar:** el guardado de `MetaConnection`/
`PhoneNumber` y el de los dos tokens siguen sin compartir una única
transacción distribuida (Postgres + `meta_private.meta_tokens` cifrado)
-- se aceptó marcar estados intermedios (`"pending"`/`"error"`) en vez de
resolver esto con un patrón de saga/outbox, que sería sobre-ingeniería
para este alcance.

**Qué haría que revisáramos esto:** si aparece un tercer flujo de alta
de conexión (ej. Instagram/Messenger BYOK) que necesite compartir lógica
con este módulo, extraer una función común de "verificar + persistir
MetaConnection+tokens" en vez de duplicar el patrón por canal.

## Impacto en el código

- `core/gateway/meta_byok.py` (nuevo).
- `core/admin/views.py` (`POST /conexiones/wizard/whatsapp-byok/guardar`,
  contexto de `GET /conexiones/wizard/whatsapp/elegir-entorno` con
  `webhook_url`/`whatsapp_verify_token`).
- `core/admin/templates/meta_wizard_elegir_entorno.html` (modal BYOK).
- `alembic/versions/0030_meta_tokens_kind_cloud_api.py`.

## Corrección post-mortem (2026-09-21): faltaba suscribir la WABA a la app

Encontrado probando en vivo: la conexión se guardaba, el handshake del
webhook pasaba (GET con `verify_token` correcto), pero **cero mensajes**
llegaban -- `webhook_events` se quedaba en 0 filas siempre. Causa: el
diseño original de este ADR no contemplaba `POST /{waba_id}/
subscribed_apps` (la suscripción de OBJETO -- distinta de la suscripción
de APP que el admin configura a mano en el App Dashboard de Meta). Sin
esa suscripción, Meta nunca entrega eventos de mensajes de esa WABA,
sin importar qué tan bien esté configurado el resto.

Corregido agregando:
- Un campo nuevo en el wizard: WABA ID (visible en la misma pantalla
  "API Setup" de Meta que ya usaban `phone_number_id`/`access_token`).
- `_subscribe_waba_to_app` -- mismo patrón que
  `meta_onboarding.py::_subscribe_waba_to_app` de Embedded Signup (POST
  + GET de verificación inmediata, nunca confiar en `{"success": true}`
  del POST solo).
- Se crea/actualiza `WabaAccount` (el diseño original de este ADR no
  contemplaba esa tabla para el camino BYOK).
- Si Meta no confirma la suscripción, la conexión queda en `"error"` en
  vez de `"connected"` -- ya no puede aparentar estar operativa sin
  poder recibir mensajes.

**Lección**: los tres módulos que tocan Meta (Embedded Signup, BYOK, y
en el futuro cualquier canal nuevo) comparten el mismo checklist real
de qué hace falta para que un mensaje efectivamente llegue -- verificar
credenciales, guardar tokens, Y suscribir el objeto correcto a la app.
Diseñar un módulo nuevo mirando solo "qué pide el formulario" en vez de
"qué necesita Meta para entregar tráfico real" es el error de fondo que
causó este bug -- se verificó en vivo contra Meta real recién después
de mergear, no antes.

## Corrección post-mortem #2 (2026-09-21): faltaba suscribir los campos de webhook a nivel de app

Con la WABA ya suscripta (corrección anterior), seguían sin llegar
mensajes. Consultando `GET /{app_id}/subscriptions` directo contra
Graph API se confirmó: la suscripción quedaba `active=true` pero **sin
ningún `fields`** -- el admin había configurado el callback_url/
verify_token a mano en el App Dashboard, pero el checkbox de
"messages" nunca quedó tildado (Meta permite guardar la URL sin
seleccionar ningún campo, sin avisar que eso la deja inútil).

Corregido agregando una llamada a
`core/gateway/meta_subscriptions.py::upsert_app_subscription` (POST
`/{app_id}/subscriptions` con `fields=messages,...`, mismo mecanismo
que ya usa Embedded Signup vía `meta_onboarding.py::
_subscribe_webhook_step`) -- ya no depende de que el admin tilde el
checkbox correcto a mano.

**Lección repetida**: dos bugs seguidos por confiar en un paso manual
de Meta (tildar checkboxes en su dashboard) en vez de forzarlo por
API cuando la API lo permite. Para cualquier flujo BYOK futuro:
preferir SIEMPRE la suscripción programática sobre pedirle al admin
que configure algo a mano en developers.facebook.com, salvo los pasos
que Meta exige que sean manuales de verdad (Business Verification, App
Review).
