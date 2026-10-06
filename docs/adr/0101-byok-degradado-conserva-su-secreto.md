# ADR-0101 — Una conexión BYOK degradada o con error sigue prestando su secreto y su verify token

- **Estado:** aceptado
- **Fecha:** 2026-10-06
- **Enmienda:**
  - ADR-0040, decisión 2, y ADR-0036, corrección 3 (donde nació el filtro): `_resolve_byok_token` filtraba
    `status in (connected, pending)`. Ahora lee también `degraded` y `error`, como respaldo, y solo conexiones
    `origin = 'byok'`.
  - ADR-0100, consecuencias: el "secreto efectivo" de un negocio en el genérico sale de la conexión BYOK que manda
    este orden, no de "la conexión viva más reciente" de cualquier origen.
- **Contexto:** hallazgo Alta del BACKLOG; lo encontró el `arquitecto` al revisar ADR-0100.

## Contexto

`resolve_byok_app_secret` (el secreto HMAC del webhook) y `resolve_webhook_verify_token` (el handshake) comparten
`core/gateway/meta_byok.py::_resolve_byok_token`, que solo leía conexiones `connected` o `pending`. Varias cosas
normales sacan una conexión BYOK de esos estados:

- **Un token por vencer.** A menos de 7 días del vencimiento, `token_valid` da falso (`meta_health.py`) y la conexión
  pasa a `error`. Toda conexión BYOK con un token temporal (24 h o 60 días) llegaba ahí con el token todavía vigente.
- **Plantillas o sync.** Una plantilla `REJECTED` o un sync de plantillas con más de 25 h la pasa a `degraded`.
- **Repetir el wizard.** El wizard BYOK sobre una conexión que andaba reusa la fila; si falla al guardar, la deja en
  `error`.

En todos los casos el negocio se quedaba sin secreto propio y caía al del `.env`. Los webhooks que Meta firma con su
app daban 401, por el endpoint por slug y por el genérico (ADR-0100), y Meta los reintentaba en vano. El
`verify_token` también caía al `.env`. Un check menor dejaba al negocio sin mensajes entrantes.

## Decisión

1. `_resolve_byok_token` acepta los estados en que la conexión sigue siendo la credencial del negocio: `pending`,
   `connected`, `degraded` y `error` (`_ESTADOS_CON_CREDENCIAL`). Meta firma con el secreto de la app aunque un check
   falle, y el estado sigue mostrando el problema en el panel.
   - El App Secret se valida contra Meta antes de guardarlo, así que el de una conexión en `error` es real.
   - Quedan fuera `revoked` (desactivada a mano con `set_connection_status` o borrada con `soft_delete_connection`),
     `draft` (sin credenciales) y las conexiones con `deleted_at`.
2. **Solo conexiones `origin = 'byok'`.** Son las únicas que guardan secreto y verify token propios. Antes, una
   conexión de Embedded Signup o manual más nueva podía ganar el orden y dejar al negocio sin el secreto de su app.
3. **Orden en tres niveles:** primero `connected`/`pending`, después `degraded` y al final `error`; dentro de cada
   nivel, `connected_at` descendente como antes. `degraded` y `error` son solo un respaldo cuando no hay ninguna
   conexión sana, así que nada de lo que hoy funciona cambia de conexión. `pending` va con `connected`, porque el
   self-check del alta lo necesita (ADR-0036).
4. **Diagnosticar y el cron de salud ya no reactivan una conexión desactivada.**
   - "Diagnosticar" (`meta_health_service.diagnose_connection`) guarda el resultado, pero no cambia el estado de una
     conexión `revoked`, `draft` o borrada. Antes la pasaba a `degraded`/`error`, y con este ADR eso le devolvería el
     secreto.
   - El cron de salud vuelve a leer el estado antes de escribir y no pisa una conexión que desactivaron durante la
     corrida.
5. **El cron de vencimiento de tokens** (`meta_token_expiry_check.py`) revisa también las conexiones en `error`. Es
   el camino más común a `error`, y el negocio sigue recibiendo mensajes: el aviso de vencimiento es lo que lo hace
   visible.

No cambian el filtro explícito por `tenant_id`, la lectura del token con `tenant_id` ni la precedencia
manual → BYOK → `.env` de `connections.py`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Aceptar `degraded`, pero no `error` | `error` es el caso más común (token por vencer) y seguiría dando 401 |
| Poner `degraded` al mismo nivel que `connected` | Una conexión degradada más nueva le ganaría a una sana; sin el filtro por origen, una de Embedded Signup dejaría al negocio sin secreto |
| `connected_at` con nulos al final en vez de filtrar por origen | Arregla una conexión de Embedded Signup abandonada en `pending`, pero no una más nueva y degradada. Además el filtro por origen sigue la regla de ADR-0094 |
| Aceptar también `revoked` | Desactivar una conexión a mano tiene que cortar la firma BYOK; si no, no hay forma de apagarla |

## Consecuencias

**Positivas:** un token por vencer, una plantilla rechazada o un wizard repetido que falla ya no cortan los mensajes
entrantes del negocio.

**Negativas / lo que aceptamos pagar:**
- Una conexión en `error` porque su token venció de verdad recibe mensajes, pero sus respuestas fallan al enviarse
  hasta que se renueve el token. El cron de vencimiento avisa al operador y el mensaje queda guardado.
- `revoked` solo corta la firma BYOK. El ruteo por número (`webhook_route_tenant`) y el sender no miran el estado: una
  conexión de Embedded Signup o manual desactivada sigue recibiendo, firmada por la plataforma, y enviando. Era así
  antes; queda en el BACKLOG.
- Una conexión en `error` no vuelve a revisarse sola (el cron de salud solo mira `connected`/`degraded`). Queda en el
  BACKLOG, con la trampa de agregarla sin más.

**Qué haría que revisáramos esto:** HU-CONN-05 (un secreto por conexión dueña del número), o un estado nuevo de
conexión.

## Impacto en el código

- `core/gateway/meta_byok.py`: `_ESTADOS_CON_CREDENCIAL`, y el filtro (estado y `origin`) y el orden de
  `_resolve_byok_token`.
- `core/admin/meta_health_service.py::diagnose_connection` y `core/tasks/meta_health_check.py::_check_one`: no
  reactivan conexiones desactivadas.
- `core/tasks/meta_token_expiry_check.py`: incluye `error`.
- Tests:
  - `tests/test_meta_byok.py`: la consulta compilada.
  - `tests/integration/test_webhook_slug.py`: `degraded`/`error` aceptadas y `revoked` rechazada, también en el
    handshake; la sana gana a la degradada y a la de `error`; Embedded Signup no le quita el secreto a la BYOK.
  - `tests/integration/test_webhook_firma_por_negocio.py`: el genérico.
  - `tests/integration/test_meta_salud_no_reactiva.py`: diagnosticar y el cron.
  - `tests/integration/test_meta_credentials_origin.py`: el aviso de vencimiento en `error`.
