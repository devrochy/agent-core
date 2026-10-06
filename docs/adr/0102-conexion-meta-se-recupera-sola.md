# ADR-0102 — Una conexión de Meta se recupera sola, y solo mejora de estado con el diagnóstico verificado

- **Estado:** aceptado
- **Fecha:** 2026-10-06
- **Enmienda:**
  - ADR-0101, consecuencias: "Una conexión en `error` no vuelve a revisarse sola" deja de ser cierto.
  - ADR-0101, decisión 5: el cron de vencimiento ya no exige que la conexión tenga números.
  - ADR-0029, actualización 2026-09-21: la conexión que representa a una app en el cron de suscripciones ya no es "la
    más antigua".
  - ADR-0034: el estado ya no es directamente el semáforo de `decide_status` (pasa por `next_status`), se suma el check
    `own_app_subscribed` y cambian las alertas.
- **Contexto:** entrada Media del BACKLOG que dejó ADR-0101. La revisión del diseño encontró además la variante
  con tokens, y la del `arquitecto`, que el filtro por números y las filas ya promovidas dejaban el problema abierto.

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

Revisar más conexiones tenía trampas, porque `_resolve_byok_token` toma la primera conexión del orden y no sigue:

- **Sin credenciales.** `run_health_checks` devuelve solo `app_subscription_active` y `billing`, y `decide_status`
  daba `connected`. "Diagnosticar" ya pasaba a `connected` un alta BYOK fallida sin token. Si era más nueva que la
  sana, ganaba el orden y el negocio se quedaba sin secreto: todo su inbound daba 401. Esas filas siguen en
  `connected` donde ya pasó.
- **Con tokens.** Reintentar el wizard BYOK con otra app crea una fila nueva que, en su primera transacción, se queda
  con el número y con un `connected_at` más nuevo; después guarda los tokens y puede fallar al suscribir. Su token es
  válido, y `webhook_subscribed` solo pide que la WABA tenga *alguna* app suscrita: la vieja, que es la que sigue
  firmando. Promoverla le quitaría el secreto a esa app.
- **La sana sin números.** Por lo mismo, después de un reintento fallido la conexión sana queda sin números. Un filtro
  "solo `error` con números" la dejaba fuera de los crons justo a ella.

## Decisión

1. **Una conexión solo mejora de estado con el diagnóstico verificado** (`meta_health.next_status`, sobre
   `decide_status`).
   - Rango: `connected`; `pending` y `degraded`; `error`. Empeorar siempre se puede. `pending` (un alta a medias) no
     está verificada: pasar a `connected` cuenta como mejora. Un estado fuera del rango (`revoked`, `draft`) no
     cambia.
   - Mejorar exige el token verificado contra Meta (`token_valid` en `True`). Si es BYOK, además *su* app suscrita:
     la suscripción de la app confirmada (`app_subscription_active` en `True`, que el cron de suscripciones refresca
     cada 15 min) y la WABA suscrita a esa app (`own_app_subscribed`, ver 3).
   - El requisito de la app es solo para BYOK: la app de la plataforma no tiene un cron que refresque su suscripción,
     y una conexión de Embedded Signup o manual nunca le gana el secreto a una BYOK (ADR-0101).
   - Si no se cumple, quedan el estado y la razón actuales. La usan el cron de salud y "Diagnosticar", sobre el estado
     de la fila leída con `FOR UPDATE`.
   - Si mientras se diagnosticaba el wizard reconectó la conexión (cambió su `origin`, su app o su `connected_at`),
     el diagnóstico es de otra credencial y se descarta (`misma_conexion`).
2. **Una BYOK sin credenciales propias falla `token_valid`.** Sin App Secret o sin token propios
   (`missing_app_secret`/`missing_access_token`), `token_valid` da `False`: nunca es legítimo (ADR-0094), y como
   empeorar siempre se puede, las altas fallidas que el "Diagnosticar" anterior dejó en `connected` bajan a `error` en
   la siguiente corrida y la sana recupera el secreto. Si es la única BYOK, sigue prestando lo que tenga (ADR-0101).
   Para el resto (Embedded Signup o manual sin la app de la plataforma configurada, un alta manual a medias),
   `token_valid` queda con `ok=None`: no empeora ni deja mejorar, y el panel muestra el motivo y "reautorizar".
3. **`own_app_subscribed`, solo BYOK: la WABA quedó suscrita a la app de esta conexión.** Sale de
   `WabaAccount.subscribed`: el alta BYOK lo pone en `False` al empezar (también cuando reusa la fila al reconectar) y
   en `True` después de un `POST /subscribed_apps` correcto; "Resuscribir la WABA" lo actualiza.
   - Alguna WABA de la conexión suscrita: `True`.
   - WABAs, pero ninguna suscrita: `False`. No es de severidad alta, así que la conexión queda `degraded`: un reintento
     con otra app que no llegó a suscribir la WABA y ya estaba en `connected` (el "Diagnosticar" anterior, o "Activar")
     baja un nivel, y la sana, `connected`, recupera el secreto. En `error` empataría con la sana y ganaría la más nueva.
   - Sin WABA cargada (las BYOK anteriores al post-mortem de ADR-0036): `None`. No degrada, pero no deja mejorar; se
     arregla repitiendo el wizard.
4. **El cron de salud revisa todas las conexiones `connected`, `degraded` y `error`,** tengan o no números. Una
   conexión abandonada en `error` alerta como mucho una vez al día hasta que alguien la elimine, y esa alerta es la
   señal para hacerlo.
5. **El cron de suscripciones revisa las apps de conexiones `connected`, `degraded` y `error`.** Sigue degradando solo
   las `connected`, ahora con `FOR UPDATE`.
   - La representativa (la que presta el secreto para consultar a Meta) es la primera con App Secret, primero las
     sanas. Antes era la más antigua, y si no tenía secreto se saltaba toda la app.
   - Si la consulta con la representativa falla (un error de Graph o un timeout al pedir el token de la app), se prueba
     con las demás conexiones de la app que tengan un secreto distinto, leyéndolo recién entonces, bajo el scope de su
     negocio. Solo si fallan todas la app queda "sin dato", con el error de la representativa; si fue un timeout, el
     estado no se toca, como antes. Así, un negocio que cargó el `app_id` de otro con su propio secreto no le traba la
     recuperación al dueño.
   - La app de la plataforma no se revisa desde este cron. Sus conexiones (Embedded Signup, manual) no guardan su
     secreto, y uno que diga ser de ella es un alta BYOK con un `app_id` ajeno: usarlo pisaría cada 15 min el estado
     que ven todos los negocios de Embedded Signup.
   - Un secreto que no se puede leer se loguea y cuenta como ausente: no tumba la corrida de todos.
6. **El sync de plantillas incluye las conexiones `degraded`, y primero las `connected`**: el candado horario es por
   WABA, y una degradada no debe ganarle el turno a la sana. No incluye `error`: cuando el cron de salud la pasa a
   `degraded`, el sync vuelve.
7. **Las alertas que un cron ve en cada corrida salen una vez al día** (`send_alert_once`, Redis `SET NX EX`; si Redis
   falla, alerta igual):
   - token inválido o vencido (una sola clave) y por vencer, por conexión; los crons de salud y de vencimiento
     comparten las claves, así cada condición avisa una vez aunque la vean los dos;
   - calidad RED, por conexión y número.

   Sin conexión (el wizard manual) no se alerta: el admin ve el error en pantalla.
8. **El diagnóstico conserva lo que no es un check** (`merge_health`): `coexistence`, que evita registrar un número que
   comparte la app de WhatsApp Business; `token_scope_check` y `webhook_subscription_active`, del alta; y el estado de
   facturación que marca un admin. Antes se borraban en cada corrida. El panel de salud solo dibuja como checks las
   entradas que lo son.
9. **El checklist de producción ignora las conexiones `revoked`, `draft` y borradas:** con la decisión 2, una BYOK
   desactivada sin credenciales lo trababa.

**Tiempos.** El estado solo mejora en la corrida del cron de salud (a los :30) o con "Diagnosticar":
- Una BYOK cuya suscripción Meta volvió a activar vuelve a `connected` en la siguiente corrida después de que el cron
  de suscripciones lo vea: hasta una hora después de eso, unos 75 min desde la reactivación.
- Una conexión con el token renovado pasa de `error` a `connected` en hasta una hora si sus plantillas están al día. Si
  no, pasa por `degraded` hasta que el sync (a los :00) las ponga al día: hasta dos horas.

**Fuera de alcance:** la causa raíz de las variantes con un reintento fallido. Un alta BYOK que crea una fila nueva y
falla debería devolver el número a la conexión anterior y dejar de prestar secreto. Mientras no lo haga, si la sana
también cae a `error`, las dos quedan en el mismo nivel de `_resolve_byok_token` y gana la más nueva; y un reintento que
sí suscribió la WABA pero falló al suscribir la app tiene `own_app_subscribed` en `True`. Va al BACKLOG (prioridad Alta)
y se arregla en `meta_byok.py` (ADR-0101/0036), no en los crons.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Sin credenciales, `decide_status` da `error` para todas (lo que sugería el BACKLOG) | En Embedded Signup y manual hay conexiones `connected` que funcionan sin credenciales en el token store: despliegues con solo `WHATSAPP_APP_SECRET`, filas `manual` del backfill 0051, la ventana de caída del alta manual. Pasarían todas a `error`. En BYOK sí se hace (decisión 2) |
| Agregar `error` al cron sin la regla de mejora | Un alta fallida sin token, o un reintento con otra app sin suscribir, pasaría a `connected` cada hora y le quitaría el secreto a la conexión sana |
| Revisar solo las conexiones `error` con números | La sana es la que pierde el número cuando un reintento falla: quedaba fuera de los tres crons |
| Que `_resolve_byok_token` siga a la fila siguiente si a la primera le falta el token | Para el `verify_token` no sirve: no tener uno propio es legítimo. Para el App Secret no alcanza: la variante con tokens igual robaría el secreto. Es defensa en profundidad para HU-CONN-05 (un secreto por conexión dueña del número) |
| Devolver el número a la conexión anterior cuando el alta falla | Es la causa raíz, pero vive en el alta BYOK, no en los crons. Fuera de alcance; en el BACKLOG |
| Una función SQL nueva para el sync de plantillas | Hace falta una migración; `admin_meta_connection_ids` ya enumera por estado |
| Incluir `error` en el sync de plantillas | Sin credenciales o con el token muerto, solo suma fallos al log; el paso por `degraded` alcanza |

## Consecuencias

**Positivas:**
- Un token renovado, una suscripción que Meta reactivó o una plantilla corregida devuelven la conexión a
  `connected` sin que nadie la toque.
- Ni "Diagnosticar" ni el cron pueden convertir un alta fallida en la conexión que presta el secreto. Las que ya lo
  hacían bajan en la siguiente corrida: a `error` si no tienen credenciales propias, a `degraded` si su WABA no está
  suscrita a su app. Queda el caso angosto de la causa raíz (arriba).
- Las alertas de un problema que sigue ahí llegan una vez al día, no cada hora.

**Negativas / lo que aceptamos pagar:**
- Una conexión de Embedded Signup o manual sin credenciales en el token store no mejora sola. Se arregla
  reautorizando, o con "Activar".
- Un `ok=None` pasajero de Meta en `token_valid` atrasa una hora la recuperación.
- Una BYOK sin WABA cargada (anterior al post-mortem de ADR-0036) no mejora sola: hay que repetir el wizard. Una con
  la WABA sin suscribir a su app queda `degraded` hasta que se la resuscribe desde el panel.
- Una conexión abandonada en `error` alerta una vez al día hasta que se la elimine.
- "Activar" (`set_connection_status`) sigue pasando a `connected` sin chequear: es a propósito y lo hace un admin. Queda
  anotado en el BACKLOG.
- El chequeo de números ya no aplica, así que las conexiones de Messenger e Instagram en `error` también se revisan.

**Qué haría que revisáramos esto:** el arreglo del alta BYOK fallida (BACKLOG), HU-CONN-05, o que
`webhook_subscribed` verifique contra Meta que la app suscrita a la WABA es la de la conexión.

## Impacto en el código

- `core/gateway/meta_health.py`: `next_status`, `misma_conexion`, `merge_health`, `check_own_app_subscribed`,
  `token_valid` sin credenciales y alertas una vez al día.
- `core/shared/alerts.py`: `send_alert_once`.
- `core/tasks/meta_health_check.py` y `core/admin/meta_health_service.py`: `next_status`, `misma_conexion` y
  `merge_health`. Además, el cron incluye `error`.
- `core/tasks/meta_subscriptions_health.py`: estados, representativa con respaldos, lectura tolerante de secretos y
  `FOR UPDATE` al degradar.
- `core/tasks/message_template_sync.py`: `_wabas_a_sincronizar`.
- `core/tasks/meta_token_expiry_check.py`: sin el chequeo de números y con `send_alert_once`.
- `core/gateway/meta_byok.py`: el alta pone `WabaAccount.subscribed` en `False` al empezar.
- `core/gateway/meta_production_readiness.py`: el checklist ignora las conexiones que no están en uso.
- `core/admin/templates/meta_salud.html`: solo dibuja checks; "Re-suscribir WABA" mientras el check no esté en verde.
- Tests:
  - `tests/test_meta_health_checks.py`: tabla de `next_status`, `merge_health`, sin credenciales por origen,
    `own_app_subscribed` y alertas;
  - `tests/test_meta_subscriptions_health_cron.py`: grupos, representativa, respaldos y estado que cambia;
  - `tests/integration/test_meta_salud_recupera.py`: recuperación, las trampas de punta a punta, la sana sin
    números, las filas ya promovidas (sin credenciales y sin la WABA suscrita), la carrera, la reconexión durante el
    diagnóstico, `coexistence`, el cron de suscripciones, el alta que resetea la WABA y el checklist de producción;
  - `tests/integration/test_admin_enum.py`: el sync de plantillas;
  - `tests/integration/test_meta_credentials_origin.py`: el cron de vencimiento sin números y una vez al día.
