# ADR-0070 — Vault unificado: los tokens OAuth de Google pasan a la conexión del tenant

- **Estado:** aceptado
- **Fecha:** 2026-09-25

## Contexto

Los tokens de Google Calendar viven cifrados en `plugin_configs` bajo la clave
`_oauth_tokens` (`packages/pack-calendar/pack_calendar/oauth.py`). Con el catálogo
de tipos de conexión (ADR-0069) la cuenta de Google pasa a ser una conexión más y
debe verse en `/admin/conexiones` como cualquier otra, con sus tokens en el mismo
vault (`channel_connections.credentials_encrypted`). Hay tenants que podrían estar
a mitad de un flujo OAuth durante el despliegue.

## Decisión

Los tokens de Google viven en la **conexión del tenant** (`channel_connections`,
tipo `google_calendar`, `provider=google`, `produces_conversations=false`,
credenciales cifradas con `SecretBox`). **No** se usa `meta_private`: ese vault es
del flujo Meta (varios tokens por conexión bajo `kind`/`subject`, AAD); acá hay un
token por conexión, que es exactamente lo que almacena una conexión.

La migración es **dual-read transitoria, sin backfill** (criterio ADR-0027): durante
una ventana, `save_tokens` escribe en ambas fuentes y `get_access_token` lee de la
conexión con fallback a `plugin_configs`; cada tenant reconcilia sola la próxima vez
que se guarda. El `callback` OAuth **conserva su URL** (`/admin/plugins/pack_calendar/oauth/callback`):
Google valida el redirect URI contra la consola de cada cliente y moverlo obligaría
a editarla en todos.

El `external_id` de la conexión es el **email de la cuenta** (se obtiene de
`oauth2/v2/userinfo`, el exchange del `code` no lo devuelve). `expires_at` se guarda
como entero (el `credentials_schema` lo tipa `integer`).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar los tokens en `plugin_configs` | La cuenta no se ve como conexión en el panel; dos modelos de credenciales |
| Usar `meta_private` | Vault especializado de Meta (`kind`/`subject`); este dominio no lo necesita |
| Mover el callback a `/calendar/oauth/callback` | Invalida el redirect registrado en Google Console de cada cliente |
| Backfill masivo de `plugin_configs` | ADR-0027: cada fila migra sola; sin ventana de riesgo |

## Consecuencias

**Positivas:** una sola fuente de credenciales por conexión; la cuenta aparece
unificada en Conexiones; el cambio es reversible (la copia vieja sigue viva).

**Negativas / lo que aceptamos pagar:** conviven dos fuentes durante la ventana
(borrar la conexión no desconecta del todo mientras exista la copia en
`plugin_configs`); se agrega una llamada a userinfo al conectar/refrescar.

**Qué haría que revisáramos esto:** que se retire `plugin_configs` como fuente
(cuando no queden tenants sin reconciliar) o que se soporten múltiples proveedores
de calendario (Outlook/CalDAV) con el mismo schema.

## Impacto en el código

- `packages/pack-calendar/pack_calendar/oauth.py`.
- Reusa `save_connection`/`resolve_connection_credentials` del core (ADR-0069).
- Sin cambios en `packages/contracts`.
