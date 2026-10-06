# ADR-0101 — Una conexión BYOK degradada o con error sigue prestando su secreto y su verify token

- **Estado:** aceptado
- **Fecha:** 2026-10-06
- **Enmienda:** ADR-0040, decisión 2: `_resolve_byok_token` filtraba `status in (connected, pending)`. Ahora también lee
  `degraded` y `error`, y prefiere las sanas.
- **Contexto:** hallazgo Alta del BACKLOG; lo encontró el `arquitecto` al revisar ADR-0100.

## Contexto

`resolve_byok_app_secret` (el secreto HMAC del webhook) y `resolve_webhook_verify_token` (el handshake) comparten
`core/gateway/meta_byok.py::_resolve_byok_token`, que solo leía conexiones `connected` o `pending`. Hay dos caminos
que dejan una conexión fuera de esos estados:

- el cron de salud (`core/tasks/meta_health_check.py`, `meta_health.decide_status`) la pasa a `degraded` si falla
  cualquier check, y a `error` si fallan `token_valid` o `app_subscription_active`;
- el alta BYOK la deja en `error` si falla al guardar las credenciales o la suscripción.

Desde ese momento el negocio se queda sin secreto propio y cae al `.env`:

- los webhooks que Meta firma con la app del negocio dan 401, por el endpoint por slug y por el genérico (ADR-0100), y
  Meta los reintenta en vano;
- el `verify_token` también cae al `.env`, así que el self-check de la suscripción falla y la conexión no sale de
  `degraded` aunque Meta se recupere.

Un check menor fallido dejaba al negocio sin mensajes entrantes.

## Decisión

`_resolve_byok_token` acepta los estados en que la conexión sigue siendo la credencial del negocio:
`pending`, `connected`, `degraded` y `error` (`_ESTADOS_CON_CREDENCIAL`). Meta firma con el secreto de la app aunque un
check de salud falle, y el estado de la conexión sigue mostrando el problema en el panel.

- **Quedan fuera:**
  - `revoked`: desactivada a mano o borrada (`meta_health_service.set_active` / `soft_delete_connection`); ahí cortar es
    intencional;
  - `draft`: Embedded Signup sin canje, no tiene credenciales;
  - las conexiones con `deleted_at`, igual que antes.
- **Orden:** primero las sanas (`connected`, `pending`, `degraded`), después las de `error`; dentro de cada grupo,
  `connected_at` descendente como antes. Si el negocio tiene una conexión que anda, una vieja en `error` no le gana.
- **No cambian:** el filtro explícito por `tenant_id`, la lectura del token con `tenant_id`, la precedencia
  manual → BYOK → `.env` de `connections.py`, ni cómo el cron de salud asigna los estados.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar `degraded`, pero no `error` | `error` es justo el caso grave (`app_subscription_active` cae y se intenta re-registrar con el self-check); seguiría dando 401 y la conexión no se recuperaría |
| Que el cron no baje el estado de la conexión, sino solo un campo de salud | Cambia el contrato de estados que usan el panel y las alertas; el problema real era la resolución de la credencial |
| Aceptar también `revoked` | Desactivar una conexión a mano tiene que cortar la entrada; si no, no hay forma de apagarla |

## Consecuencias

**Positivas:** un check de salud fallido ya no corta los mensajes entrantes del negocio, y el self-check de la
suscripción puede volver a pasar, así que la conexión puede salir de `degraded`.

**Negativas / lo que aceptamos pagar:**
- Una conexión en `error` por `token_valid` recibe mensajes, pero sus respuestas fallan al enviarse hasta que se
  renueve el token. Es mejor que perder el mensaje: queda guardado y la conversación se ve en el panel.
- El cron de salud solo revisa `connected`/`degraded` (`meta_health_check.py`): una conexión en `error` no vuelve a
  revisarse y queda en ese estado hasta que alguien la reactive. Queda en el BACKLOG.

**Qué haría que revisáramos esto:** HU-CONN-05 (un secreto por conexión dueña del número), o un estado nuevo de
conexión.

## Impacto en el código

- `core/gateway/meta_byok.py`: `_ESTADOS_CON_CREDENCIAL` y el filtro y el orden de `_resolve_byok_token`.
- Tests: `tests/test_meta_byok.py` (la consulta compilada) y `tests/integration/test_webhook_slug.py`. Este cubre
  `degraded`/`error`, `revoked`, el handshake y la conexión sana contra la de `error`.
  `tests/integration/test_webhook_firma_por_negocio.py` cubre el genérico.
