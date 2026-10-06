# ADR-0102 — Una conexión de Meta se recupera sola, y solo mejora de estado con el diagnóstico verificado

- **Estado:** aceptado
- **Fecha:** 2026-10-06
- **Enmienda:** ADR-0101, consecuencias. "Una conexión en `error` no vuelve a revisarse sola" deja de ser cierto.
- **Contexto:** entrada Media del BACKLOG que dejó ADR-0101. La revisión del diseño encontró además la variante
  con tokens (abajo).

## Contexto

Desde ADR-0101 una conexión `degraded` o `error` sigue prestando su secreto y recibe mensajes, pero su estado no
volvía solo a `connected`:

- **El cron de salud** (`meta_health_check.py`, cada hora) solo revisaba `connected`/`degraded`. Una conexión en
  `error` (el caso más común: un token a menos de 7 días de vencer) quedaba así aunque se extendiera el token.
- **El cron de suscripciones** (`meta_subscriptions_health.py`, cada 15 min) solo revisaba las apps de conexiones
  `connected`. Cuando Meta desactiva la suscripción de una app, ese cron degrada sus conexiones; después no volvía a
  mirar la app, el `active=False` quedaba viejo y el cron de salud la dejaba en `error` para siempre.
- **El sync de plantillas** (`message_template_sync.py`) solo sincronizaba conexiones `connected`. Una `degraded`
  dejaba de sincronizar, y a las 25 h el check de plantillas fallaba: no salía nunca de `degraded`.

Revisar más conexiones tenía dos trampas:

- **Sin credenciales.** `run_health_checks` devuelve solo `app_subscription_active` y `billing`, y `decide_status`
  daba `connected`. "Diagnosticar" ya pasaba a `connected` un alta BYOK fallida sin token. Si era más nueva que la
  sana, ganaba el orden de `_resolve_byok_token`, que toma la primera fila y no sigue, y el negocio se quedaba sin
  secreto: todo su inbound daba 401.
- **Con tokens.** Reintentar el wizard BYOK con otra app crea una fila nueva, que se queda con el número y con un
  `connected_at` más nuevo, guarda los tokens y puede fallar al suscribir. Su token es válido, y `webhook_subscribed`
  solo pide que la WABA tenga *alguna* app suscrita: la vieja, que es la que sigue firmando. Promoverla le quitaría el
  secreto a esa app.

## Decisión

1. **Una conexión solo mejora de estado con el diagnóstico verificado** (`meta_health.next_status`, sobre
   `decide_status`).
   - Rango: `pending`/`connected`, `degraded`, `error`. Empeorar siempre se puede.
   - Mejorar exige el token verificado contra Meta (`token_valid` en `True`). Si es BYOK, además la suscripción de
     *su* app confirmada (`app_subscription_active` en `True`), que el cron de suscripciones refresca cada 15 min.
   - El requisito de la app es solo para BYOK: la app de la plataforma no tiene un cron que refresque su suscripción,
     y una conexión de Embedded Signup o manual nunca le gana el secreto a una BYOK (ADR-0101).
   - Si no se cumple, quedan el estado y la razón actuales. La usan el cron de salud y "Diagnosticar", sobre el
     estado de la fila leída con `FOR UPDATE`, no el de la lista que armó el cron al empezar.
   - Sin credenciales, `token_valid` queda con `ok=None`, el motivo y "reautorizar", y el panel lo muestra.
2. **El cron de salud revisa también las conexiones en `error` que todavía tienen números.** Un alta abandonada no se
   revisa cada hora; es el criterio de ADR-0101 para el cron de vencimiento, ahora en un helper compartido
   (`admin_enum.tiene_numeros`).
3. **El cron de suscripciones revisa las apps de conexiones `connected`, `degraded` y `error` con números.** Sigue
   degradando solo las `connected`. La conexión que representa a la app (la que presta el secreto para consultar a
   Meta) es la primera con App Secret, primero las sanas; antes era la más antigua, y si no tenía secreto se saltaba
   toda la app.
4. **El sync de plantillas incluye las conexiones `degraded`, y primero las `connected`**: el candado horario es por
   WABA, y una degradada no debe ganarle el turno a la sana. No incluye `error`: cuando el cron de salud la pasa a
   `degraded`, el sync vuelve. Una conexión con el token renovado llega a `connected` en dos o tres horas.
5. **Las alertas que el cron ve en cada corrida salen una vez al día**: token inválido y por vencer, por conexión y
   cada una con su clave, y calidad RED, por número. Sin conexión (el wizard manual) no se alerta: el admin ve el error
   en pantalla. Si Redis falla, se alerta igual.
6. **El diagnóstico conserva lo que no es un check** (`merge_health`): `coexistence`, que evita registrar un número que
   comparte la app de WhatsApp Business, y el estado de facturación que marca un admin. Antes se borraban en cada
   corrida.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Sin credenciales, `decide_status` da `error` (lo que sugería el BACKLOG) | Hay conexiones `connected` que funcionan sin credenciales en el token store: despliegues con solo `WHATSAPP_APP_SECRET`, filas `manual` del backfill 0051, la ventana de caída del alta manual. Pasarían todas a `error` |
| Agregar `error` al cron sin la regla de mejora | Un alta fallida sin token, o un reintento con otra app sin suscribir, pasaría a `connected` cada hora y le quitaría el secreto a la conexión sana |
| Que `_resolve_byok_token` siga a la fila siguiente si a la primera le falta el token | No tener `verify_token` propio es legítimo, y la variante con tokens igual robaría el secreto. Es defensa en profundidad para HU-CONN-05 (un secreto por conexión dueña del número) |
| Una función SQL nueva para el sync de plantillas | Hace falta una migración; `admin_meta_connection_ids` ya enumera por estado |
| Incluir `error` en el sync de plantillas | Sin credenciales o con el token muerto, solo suma fallos al log; el paso por `degraded` alcanza |

## Consecuencias

**Positivas:**
- Un token renovado, una suscripción que Meta reactivó o una plantilla corregida devuelven la conexión a
  `connected` sin que nadie la toque.
- Ni "Diagnosticar" ni el cron pueden convertir un alta fallida en la conexión que presta el secreto.
- Las alertas de un problema que sigue ahí llegan una vez al día, no cada hora.

**Negativas / lo que aceptamos pagar:**
- Una conexión sin credenciales en el token store no mejora sola. Se arregla reautorizando, o con "Activar".
- Un `ok=None` pasajero de Meta en `token_valid` atrasa una hora la recuperación.
- Una conexión BYOK espera hasta 15 min, la siguiente corrida del cron de suscripciones, para volver a `connected`.
- "Activar" (`set_connection_status`) sigue pasando a `connected` sin chequear: es a propósito y lo hace un admin. Queda
  anotado en el BACKLOG.

**Qué haría que revisáramos esto:** HU-CONN-05, o que `webhook_subscribed` verifique que la app suscrita a la WABA es
la de la conexión. Con eso, el requisito de la app en BYOK podría salir de ese check.

## Impacto en el código

- `core/gateway/meta_health.py`: `next_status`, `merge_health`, `token_valid` sin credenciales, alertas una vez al
  día.
- `core/shared/alerts.py`: `send_alert_once`.
- `core/db/admin_enum.py`: `tiene_numeros`.
- `core/tasks/meta_health_check.py`, `core/admin/meta_health_service.py`: `next_status` y `merge_health`; el cron
  incluye `error`.
- `core/tasks/meta_subscriptions_health.py`: estados y representativa.
- `core/tasks/message_template_sync.py`: `_wabas_a_sincronizar`.
- `core/tasks/meta_token_expiry_check.py`: usa `tiene_numeros`.
- Tests:
  - `tests/test_meta_health_checks.py`: tabla de `next_status`, `merge_health`, sin credenciales, alertas;
  - `tests/test_meta_subscriptions_health_cron.py`: grupos y representativa;
  - `tests/integration/test_meta_salud_recupera.py`: recuperación, las dos trampas de punta a punta, la carrera,
    `coexistence` y el cron de suscripciones;
  - `tests/integration/test_admin_enum.py`: el sync de plantillas.
