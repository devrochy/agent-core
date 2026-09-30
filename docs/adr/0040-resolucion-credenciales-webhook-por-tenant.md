# ADR-0040 — Resolución de credenciales de webhook por tenant (manual → BYOK → `.env`)

- **Estado:** aceptado
- **Fecha:** 2026-09-22
- **Extiende:** ADR-0037 (verify_token propio por conexión BYOK)

## Contexto

El webhook por tenant necesitaba validar tres cosas con credenciales **por tenant**:
la firma HMAC (`app_secret`), el `verify_token` del handshake, y el `access_token` con
el que se **envían** las respuestas. Los tres se resolvían con
`core/gateway/connections.py::effective_credentials()`, que cae al `.env` cuando no hay
una `ChannelConnection` manual.

`effective_credentials()` **incluye el fallback al `.env` como una credencial más**, así
que en un proceso con `WHATSAPP_APP_SECRET`/`WHATSAPP_VERIFY_TOKEN` globales ese fallback
siempre "gana" y las credenciales de la app **BYOK del tenant** (`MetaConnection` +
`meta_token_store`) nunca se consultan. Síntomas reales en vivo:

- El handshake `GET /webhooks/whatsapp/<slug>` respondía **403** al self-check del wizard
  BYOK (validaba con el token global, no con el del wizard).
- El `POST` del webhook rechazaba entregas legítimas de Meta con **401** (firmaba con el
  App Secret real; se validaba con el global).
- El sender usaba el `kind`/`subject` de Embedded Signup (`business_integration_system_user`
  / `business_id`), que para BYOK no existe: caía al fallback del `.env` y enviaba con
  otras credenciales.

## Decisión

1. **Resolvers explícitos con orden documentado**, en `core/gateway/connections.py`:
   - `resolve_effective_verify_token(tenant_id)` y `resolve_effective_app_secret(tenant_id)`:
     **`ChannelConnection` (manual) → BYOK (`MetaConnection`) → `.env`**.
   - Ninguno usa `effective_credentials()` para decidir si hay credencial manual: resuelven
     la manual con `resolve_primary_credentials()` (que devuelve `None` si no hay conexión,
     no el fallback del `.env`).
2. **BYOK por `kind`/`subject`** (`core/gateway/meta_byok.py`):
   `resolve_webhook_verify_token` y `resolve_byok_app_secret` comparten `_resolve_byok_token`,
   que filtra `tenant_id` explícito + `status in (connected, pending)` + `deleted_at is null`,
   y lee el token con `subject = connection.meta_app_id`.
3. **Sender** (`core/gateway/sender.py`): resuelve el access token probando primero
   `cloud_api_access_token` (BYOK, `subject=meta_app_id`) y luego el `TOKEN_KIND` de
   Embedded Signup (`subject=business_id or connection_id`), con el chequeo de
   `expected_tenant_id` intacto.
4. **Registro y verificación resuelven igual.** La ruta genérica
   `/admin/conexiones/webhooks/registrar` es la excepción deliberada: apunta al webhook
   genérico sin slug, cuyo handler solo conoce el token del `.env`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Envolver el fallback del `.env` como "conexión virtual" dentro de `effective_credentials()` | Sigue sin distinguir "hay credencial manual" de "cayó al `.env`"; no arregla el enmascaramiento. |
| Resolver el `app_secret` probando todas las variantes de firma | Meta no expone qué app firmó; mezclar secretos de varios tenants es peor que una resolución determinística. |
| Priorizar el verify_token de cualquiera de las dos fuentes por "el que valide" | Rompe la precedencia (una conexión manual debe ganar) y oculta configuraciones inconsistentes. |

## Consecuencias

- El `tenant_id` de resolución siempre es explícito (nunca del cuerpo) — defensa en
  profundidad más allá de RLS.
- Cualquier ruta futura que registre una suscripción apuntando a la URL **por tenant**
  (`tenant_webhook_url(slug)`) debe registrar el token que devuelve
  `resolve_effective_verify_token`, o Meta quedará registrado con un token que el
  handshake rechaza.
- Tests: `tests/integration/test_webhook_slug.py` (firma BYOK aceptada/rechazada,
  precedencia manual > BYOK > `.env`, aislamiento cross-tenant) y
  `tests/test_meta_byok.py`.
