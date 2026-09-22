# ADR-0041 — Asistente de token del wizard BYOK (extender/verificar) y expiración

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

El wizard BYOK pedía un "Access Token (temporal)" a secas. Los tokens temporales de
App Dashboard vencen (24 h típico); cuando vencen, **el agente deja de responder en
silencio**: el envío empieza a fallar con `OAuthException` 190/463
("Session has expired") y nada lo avisa antes. Pasó en vivo dos veces.

Meta, además, tiene una limitación que no se puede sortear por API:

- **No existe un endpoint público para *generar* un System User token** — se crea en
  Business Settings (UI). Un botón "generar token permanente" 100 % automático no es
  posible.
- Sí existen, por API: **`GET /debug_token`** (validez, `expires_at` — `0` = no expira,
  scopes, tipo) y **`GET /oauth/access_token?grant_type=fb_exchange_token`**
  (canjea un token temporal por uno long-lived, ~60 días).

## Decisión

1. **Asistente en el wizard** (modal BYOK de `/admin/conexiones` y paso de WhatsApp del
   alta de tenant), con dos modos:
   - *Temporal*: pegar → **"Extender a 60 días"** (`fb_exchange_token`) → **"Verificar"**
     (`debug_token`) muestra *válido / no expira / vence el… / permisos WhatsApp*.
   - *Permanente (System User)*: instrucciones + link a Business Settings; al verificar
     debe decir **"no expira"** y traer los scopes `whatsapp_business_*`.
2. **`core/gateway/meta_token_tools.py`**: `inspect_token` y `extend_token`, con parseo
   tolerante (nunca levanta por un campo faltante; sí clasifica `GraphError`). Nunca se
   loguea el token.
3. **Rutas admin** `POST /admin/conexiones/wizard/whatsapp-byok/token/inspect|extend`
   (autenticadas, `Cache-Control: no-store`, sin loguear el token).
4. **Persistir `expires_at`** del access token al guardar una conexión BYOK
   (`save_byok_connection` → `meta_token_store.put_token(expires_at=…)`), best-effort.
5. **Visibilidad y aviso:** badge de vencimiento en `/admin/conexiones/salud`
   (`get_token_expires_at`, sin descifrar el token) y cron diario
   `core.meta_token_expiry_check` que alerta tokens vencidos o por vencer (≤ 7 días).
6. **Mensaje claro** para `190/463` en `verify_meta_credentials`
   ("El access token está vencido o es inválido: … Extender a 60 días / System User").

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Solo documentar "generá un System User token" | Deja la causa raíz (vencimiento silencioso) sin mitigar y sin verificación. |
| Auto-generar un System User token por API | No existe endpoint público; inviable. |
| Renovar tokens automáticamente con un cron | Un token vencido **no se puede canjear** por API (`fb_exchange_token` exige que no haya expirado) → no es recuperable sin intervención. |

## Consecuencias

- El operador ve la expiración en el panel y recibe alerta antes de que rompa.
- La ruta de Embedded Signup (OAuth) obtiene sus tokens aparte; este asistente aplica al
  flujo BYOK (app propia).
- `extend_token` usa `httpx` directo a propósito (endpoint público de tokens, sin
  `appsecret_proof`): **no** queda en `graph_api_calls`/`graph_api_usage` (registrado en
  `docs/BACKLOG.md`).
