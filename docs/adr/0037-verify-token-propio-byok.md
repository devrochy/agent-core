# ADR-0037 — verify_token propio por conexión BYOK en el handshake del webhook

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

El paso de webhook del wizard BYOK (ADR-0036) mostraba
`settings.whatsapp_verify_token` (global) como valor de solo lectura
para que el admin lo pegue en Meta -- pero era puramente informativo:
el handshake real (`core/gateway/webhooks.py`) resuelve el
`verify_token` esperado vía `effective_credentials()`
(`core/gateway/connections.py`), que solo mira `ChannelConnection`
(modelo legado) y, si no hay, cae al `.env`. **Nunca miraba
`MetaConnection`** -- así que para una conexión BYOK, el valor que el
admin pegara en Meta tenía que coincidir por casualidad con el `.env`
global para que el handshake funcionara. Agregar un botón "generar
código" sin resolver esto habría sido cosmético.

## Decisión

1. **`meta_token_store` gana un tercer `kind`: `webhook_verify_token`**
   (migración `0031`, mismo mecanismo que `0025`/`0030`). Opcional: si
   el admin deja el campo vacío en el wizard, no se guarda nada y el
   comportamiento es idéntico al de antes (cae al `.env` global).
2. **`core/gateway/meta_byok.py::resolve_webhook_verify_token(tenant_id)`**
   nueva función: busca la `MetaConnection` de WhatsApp `connected` más
   reciente del tenant y lee su `webhook_verify_token` si existe.
   Filtra por `tenant_id` explícito (no confía solo en RLS), mismo
   criterio que el resto del módulo.
3. **`core/gateway/webhooks.py`, SOLO la ruta por slug** (`GET
   /webhooks/whatsapp/{slug}`) agrega este tercer nivel de resolución,
   DESPUÉS de `ChannelConnection` y ANTES del `.env`:
   `ChannelConnection` (conexión manual legada) → `MetaConnection` BYOK
   (verify_token propio) → `.env` (global). La ruta genérica (`GET
   /webhooks/whatsapp`, sin slug) NO se toca -- no tiene forma de saber
   a qué tenant pertenece la request, así que no puede resolver una
   `MetaConnection` específica.
4. **El campo del wizard deja de ser readonly**: input editable +
   botón "Generar código" (JS inline, `crypto.getRandomValues`, sin
   dependencias nuevas) que sugiere un valor aleatorio de 32 caracteres
   hex; el admin puede aceptarlo, escribir el suyo, o dejarlo vacío.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar el `verify_token` como columna plana en `MetaConnection` | No es más sensible que un token de acceso, pero romper la convención de "todo secreto de Meta vive cifrado en `meta_token_store`" para este caso particular generaría inconsistencia sin ganar nada. |
| Resolver el `verify_token` por `external_id`/`phone_number_id` en vez de "la conexión connected más reciente del tenant" | El handshake GET de Meta no manda el `phone_number_id` (ocurre antes de que exista ningún mensaje) -- no hay con qué desambiguar entre varias conexiones del mismo tenant en este punto; se documenta como limitación conocida (ver Consecuencias). |

## Consecuencias

**Positivas:** el botón "generar código" del wizard ahora tiene efecto
real -- el valor generado/escrito se usa de verdad en el handshake.

**Negativas / lo que aceptamos pagar:** si un tenant tiene MÁS DE UNA
`MetaConnection` de WhatsApp `connected` (ej. una de prueba y otra de
producción, o dos BYOK), `resolve_webhook_verify_token` solo mira la
más reciente (`connected_at desc`) -- no hay forma de distinguir cuál
conexión corresponde al request de handshake en este punto (Meta no
manda el `phone_number_id`/`app_id` en el GET de verificación). Hoy no
es un problema práctico porque el modo prueba crea como máximo una
conexión BYOK activa por tenant, pero queda documentado.

**Qué haría que revisáramos esto:** si un tenant necesita múltiples
conexiones BYOK activas simultáneas con `verify_token` distintos cada
una, hay que buscar otra forma de desambiguar en el handshake (por
ejemplo, exigir que el callback URL incluya un identificador de
conexión, no solo el slug del tenant).

## Impacto en el código

- `core/gateway/meta_byok.py` (`WEBHOOK_VERIFY_TOKEN_KIND`,
  `resolve_webhook_verify_token`, parámetro nuevo en
  `save_byok_connection`).
- `core/gateway/webhooks.py` (`GET /webhooks/whatsapp/{slug}`).
- `core/admin/views.py` (`POST .../guardar` pasa el campo nuevo).
- `core/admin/templates/meta_wizard_elegir_entorno.html` (campo
  editable + botón "Generar código", paso nuevo de Business Portfolio,
  recuadro guía de creación de app).
- `alembic/versions/0031_meta_tokens_kind_webhook_verify.py`.
