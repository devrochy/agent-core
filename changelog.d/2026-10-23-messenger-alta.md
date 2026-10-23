## Messenger: alta con Facebook Login, Páginas elegidas y plan (ADR-0152, HU-CHAN-01) (2026-10-23)

Tercer paso del rescate de Messenger: el alta por la API. Sin la UI del panel (siguiente), así que el tipo
`messenger_meta` sigue `available = false`.

- **API** (`/api/v1/admin/meta/messenger`, propietario o master): `GET` (app configurada y plan), `POST /attempt`
  (intento de un solo uso, 15 min), `POST /discover` (canjea el `code` y lista las Páginas con su elegibilidad; sin
  tokens) y `POST /connect` (las Páginas elegidas). Errores con código estable (`invalid_attempt`,
  `plan_channel_not_included` 409, `insufficient_role`, `asset_claimed`, `page_not_accessible`, `user_token_expired`).
  Contrato en `docs/openapi.json` (+4 rutas, +9 esquemas).
- **Servicio** `core/gateway/meta_messenger_alta.py`: por cada Página, en una transacción, reclamo con candado
  (un solo dueño), fila, conexión canónica `messenger_meta` e instancias de agentes; luego token cifrado de la
  Página y suscripción del webhook VERIFICADA con un GET. La cuenta nace en `draft` y sale de ahí con la primera
  Página. El token del usuario dura una hora y se revoca al terminar.
- **Plan:** Messenger e Instagram en todos los planes de pago (decisión del dueño); Free solo WhatsApp.
  `features.canales` de Basic, Premium y Empresarial.
- **Nueva variable `META_MESSENGER_CONFIG_ID`** (opcional hasta activar Messenger): Login Configuration de Facebook Login for Business con
  los permisos de Páginas. Distinta de `META_CONFIG_ID` (Embedded Signup de WhatsApp); sin ella el alta de Messenger no está disponible.
  Documentada en `docs/despliegue-coolify.md`.
- `meta_onboarding._fetch_exchange_token` acepta `redirect_path` (el reintento con `redirect_uri` apuntaba siempre a
  la ruta de WhatsApp); por defecto no cambia.
