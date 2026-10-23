## Instagram Login, refresco del token y prueba de humo (ADR-0152, HU-CHAN-04, entrega 4b) (2026-10-24)

Segunda ruta de conexión de Instagram: la persona autoriza su cuenta profesional directamente con Instagram (sin Página de
Facebook). **Apagada por defecto** (`INSTAGRAM_LOGIN_ENABLED`): se prende tras probarla con una cuenta real en test.

- **Alta** (`POST /api/v1/admin/meta/instagram/login/start` y `/login/complete`, propietario o master): `state` = intento de
  un solo uso atado al negocio y al admin; canje del código → token de 60 días → cuenta; una cuenta, un dueño; conexión
  canónica `instagram_meta`; suscripción verificada. Errores: `instagram_login_unavailable` 409, `invalid_attempt` 400,
  `asset_claimed`, `manual_connection_conflict`, `plan_channel_not_included`.
- **Envío** por `graph.instagram.com/{ig_user_id}/messages` con el token de la cuenta; un token vencido no envía.
- **Refresco diario** (`core.instagram_token_refresh`, 03:30): solo tokens de ≥ 24 h a ≤ 10 días de vencer; si Meta lo rechaza,
  la cuenta queda `degraded` sin reintentar en el ciclo y vuelve a `connected` cuando el refresco funciona.
- **Cuentas y prueba de humo:** `GET /accounts` (estado, ruta, vencimiento del token; sin tokens) y
  `POST|GET /accounts/{ig_user_id}/diagnostic` (¿llegó un mensaje desde que empezó la prueba? si no, `likely_cause`).
- **Nuevas variables:** `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_LOGIN_REDIRECT_URI`, `INSTAGRAM_LOGIN_ENABLED`
  (documentadas en `docs/despliegue-coolify.md`). El webhook acepta además el secreto de la app de Instagram como secreto de
  plataforma.
- `meta_token_store.get_token(respect_expiry=True)`: el token del usuario del alta de Messenger e Instagram ya no sirve vencido.
- `GET /api/v1/admin/meta/instagram` suma `login_available` y `login_missing`.
