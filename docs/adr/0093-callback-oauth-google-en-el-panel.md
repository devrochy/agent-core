# ADR-0093 — El retorno OAuth de Google Calendar entra por el panel

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Supersede (parcial):** ADR-0070 — solo la decisión de conservar el callback en `/admin/plugins/pack_calendar/oauth/callback`. El resto de ADR-0070 (tokens en la conexión del negocio) sigue vigente.
- **Contexto:** HU-CAL-07 (SPEC-24, `solvershub-docs`), decisiones D-SPEC-24-11 y D-SPEC-24-12

## Contexto

Al retirar el panel clásico (ADR-0076) el callback pasó a la API, pero la app armaba
`/api/v1/pack/pack_calendar/oauth/callback` cuando el router está en `/api/v1/pack/calendar`
(`pack_calendar/oauth.py`, `admin_api.py`). Además, en los entornos con túnel todo `/api/v1/*` lo
atiende el panel (`docs/despliegue-coolify.md`), el callback llegaba sin sesión (no sabía a qué negocio
guardar los tokens) y el flujo no usaba `state`. En test: `redirect_uri_mismatch` y luego 404.

## Decisión

El retorno de Google entra por una ruta BFF del panel, `<PANEL>/api/calendar/oauth/callback`, que
llama a la API `/api/v1/pack/calendar/oauth/callback` con la sesión y el negocio del usuario; la API
exige principal (`pack_calendar:conexion`, escritura) y valida un `state` aleatorio.

- `panel_redirect_uri()` = `{PANEL_BASE_URL or PUBLIC_BASE_URL}/api/calendar/oauth/callback`: es la URI
  que se registra en Google Cloud Console y la que se usa en la autorización y en el intercambio.
- `/oauth/start` genera `state = secrets.token_urlsafe(32)` y deja las cookies httponly `oauth_state`
  y `oauth_next` (`SameSite=Lax`, 10 min); el BFF las reenvía al navegador.
- `/oauth/callback` compara `state` con la cookie (`hmac.compare_digest`); sin cookie o distinto, no
  intercambia. Borra las cookies al terminar y vuelve a `<PANEL><next>?google=ok|error|cancelado`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Regla del túnel que mande el callback a la API | Configuración por entorno fuera del repo; el callback seguiría sin sesión ni negocio |
| Revivir `/admin/plugins/pack_calendar/oauth/callback` | El panel SSR se retiró (ADR-0076); tampoco resuelve el negocio |
| `state` firmado (HMAC) sin cookie | Manejo y rotación de clave para el mismo resultado |

## Consecuencias

**Positivas:** la conexión funciona desde el panel nuevo; los tokens quedan siempre en el negocio de
quien conectó; el flujo queda protegido contra CSRF.

**Negativas / lo que aceptamos pagar:** cada negocio con credenciales propias de Google debe registrar
la URI del panel de su entorno; las URIs registradas para el flujo clásico dejan de servir.

**Qué haría que revisáramos esto:** que el panel y la API dejen de compartir dominio (la cookie
`oauth_state` debería emitirse en el dominio del panel), o que haya que conectar Google desde la app
de campo.

## Impacto en el código

- `packages/pack-calendar/pack_calendar/oauth.py` (`panel_redirect_uri`, `state` en la URL).
- `packages/pack-calendar/pack_calendar/admin_api.py` (`/oauth/start`, `/oauth/callback`), `docs/openapi.json`.
- `solvershub-panel`: `app/api/calendar/oauth/callback/route.ts`.
