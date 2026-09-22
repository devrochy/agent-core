# ADR-0043 — Alta de tenant conecta WhatsApp por el flujo BYOK

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

El wizard de alta (`/admin/tenants/nuevo`) tenía un paso "Conectar WhatsApp" que pedía
`phone_number_id`, `app_secret`, `access_token` y `verify_token`, validaba con
`verify_meta_credentials(...)` y luego pasaba esos datos a `onboard_tenant(whatsapp=…)`,
que crea una **`ChannelConnection` (modelo legacy)**.

Eso divergía del flujo que el panel usa en `/admin/conexiones` (wizard BYOK →
`MetaConnection` + `save_byok_connection`), que además:

- **suscribe la WABA a la app** y registra la **suscripción de objetos a nivel de app**
  (`upsert_app_subscription`) con la URL del webhook del tenant;
- guarda los secretos en `meta_token_store` (cifrados, con `expires_at`);
- corre el self-check del handshake antes de dar la conexión por buena.

Un tenant creado con WhatsApp por el paso legacy quedaba **sin suscripción** (no recibía
mensajes) y sin el asistente de token (ADR-0041). Además, al no pedir `App ID` ni
`WABA ID`, no había forma de completar el flujo BYOK.

## Decisión

El paso de WhatsApp del alta usa el **flujo BYOK**:

1. La vista `POST /admin/tenants/nuevo` crea el tenant con `onboard_tenant(whatsapp=None)`
   (sin `ChannelConnection`).
2. Si `connect_whatsapp`, dentro de `tenant_scope(tid)` llama a
   **`save_byok_connection(...)`** (mismo flujo que `/admin/conexiones`).
3. El template agrega **App ID** y **WABA ID** y el **asistente de token**
   (Extender/Verificar) en el campo de access token.
4. **`environment` fijo en `sandbox`** (el alta es modo prueba; producción va por el flujo
   de Embedded Signup / promoción).
5. Si el WhatsApp falla, **el tenant ya creado se conserva** y se avisa con un flash que
   redirige a `/admin/conexiones` para reintentar (no se aborta el alta).
6. `onboard_tenant(whatsapp=…)` (ChannelConnection legacy) **queda** para CLI/tests; el
   panel deja de usarlo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Sacar el paso de WhatsApp del alta y redirigir siempre a `/admin/conexiones` | Menos integrado; el usuario igual tiene que completar el alta y luego conectar. Se prefirió mantenerlo en el mismo wizard. |
| Seguir con `ChannelConnection` y agregar la suscripción aparte | Duplica la lógica de `save_byok_connection` y mantiene dos modelos de conexión en paralelo. |
| Abortar el alta si falla el WhatsApp | Peor UX: el tenant es válido aunque la conexión falle; se pierde el trabajo de alta. |

## Consecuencias

- Un alta con WhatsApp queda **lista para recibir mensajes** (suscripción incluida).
- El modelo de conexión del panel es uno solo: `MetaConnection`.
- La redirección del alta cambia: éxito → `/admin/spec`; fallo de WhatsApp → `/admin/conexiones`.
- Tests: `tests/integration/test_tenant_isolation.py`
  (`test_alta_con_whatsapp_usa_flujo_byok`, `test_alta_con_whatsapp_fallido_deja_tenant_y_avisa`).
