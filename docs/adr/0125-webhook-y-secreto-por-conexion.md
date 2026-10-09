# ADR-0125 — Webhook y secreto por conexión: cada grupo se verifica con la conexión dueña de sus activos

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Supersede/enmienda:**
  - enmienda ADR-0100 §1 y §2: el secreto propio ya no autoriza a un **negocio** sino a los **grupos** cuyos
    activos son de esa **conexión**. `Firmantes` guarda los grupos autorizados; un grupo de otra conexión del mismo
    negocio se rechaza con `signature_connection_mismatch` (sin alerta) y uno de otro negocio, como antes, con
    `signature_tenant_mismatch` (con alerta). La consecuencia negativa "un negocio con varias conexiones solo
    autoriza con su secreto efectivo" deja de existir;
  - enmienda ADR-0101 §2 y §3: el orden de `_resolve_byok_token` ya no decide el secreto del webhook (solo el
    `verify_token` del self-check del preflight). Los estados que prestan el secreto (`ESTADOS_CON_CREDENCIAL`,
    con `revoked` al final de ADR-0122) no cambian;
  - enmienda ADR-0040 §1 y §4: el handshake por slug acepta el `verify_token` de cada conexión, no uno solo por
    precedencia; "Registrar webhook" ya no apunta siempre al genérico ni cae a la app de la plataforma. Quedan
    superadas su alternativa rechazada "priorizar el verify_token por el que valide" y su consecuencia "registrar el
    token de `resolve_effective_verify_token`" (ahora cada conexión registra el suyo);
  - enmienda ADR-0094: registrar la suscripción de la app sigue el origen de la conexión, como el resto de las
    llamadas a Graph (`meta_credentials.app_credentials_for`). Dos excepciones deliberadas, en §7;
  - enmienda ADR-0122 ("queda pendiente"): revisar los `subscribed_apps` de la WABA al reclamarla se pospone (§8).
- **Contexto:** HU-CONN-05 (issue agent-core #337), SPEC-08 · Hito 0A. Defecto 4 y "una sola app por negocio" de la
  auditoría de Conexiones (2026-10-01): `meta_byok.py:311-327` (un secreto por negocio) y
  `meta_health_service.py:206-216` (registrar por canal, con la app de la plataforma como respaldo).

## Contexto

1. **Un secreto por negocio.** La firma de los dos endpoints del webhook se verificaba con "el" secreto propio del
   negocio (`connections.resolve_own_app_secret`: la conexión manual primaria o la BYOK que ganaba el orden de
   ADR-0101). Con dos apps en el mismo negocio (dos BYOK, o una manual y una BYOK) los mensajes de la que no ganaba
   daban 401, y Meta los reintentaba en vano. Los planes Premium y Empresarial necesitan varias conexiones.
2. **El secreto del negocio autorizaba cualquier número del negocio.** Por el slug, además, un número que no era de
   ninguna conexión se le atribuía al negocio de la URL a ciegas.
3. **Registrar el webhook por canal.** `register_channel_subscription` tomaba "la" app del negocio y, si no tenía
   una propia, la de la **plataforma** (`.env`): el dueño de un negocio con Embedded Signup re-registraba la app que
   comparten todos los negocios. Para BYOK registraba la URL genérica con el token global, no la del negocio.
4. **El handshake aceptaba un solo `verify_token`.** La segunda app del negocio no podía re-verificar su
   suscripción.

## Decisión

1. **La conexión dueña elige el secreto** (`core/gateway/webhook_secrets.py::secretos_del_grupo`). La firma se
   valida antes de saber con certeza de quién es el evento, así que el cuerpo solo dice **qué activos** trae y la
   base decide con qué secreto se verifica:
   - el negocio del grupo sale de `route_event` (resolvedores `SECURITY DEFINER`, ADR-0095), sin cambios;
   - las conexiones candidatas son las de **ese** negocio dueñas de los activos del grupo, y en WhatsApp **manda el
     número**, que es lo que se procesa como entrante:
     - número de una conexión de Meta (`phone_numbers`): su conexión, y la de la WABA del número solo si es la WABA
       del payload (Meta firma con la app suscrita a la WABA y el número pudo pasarse de conexión dentro de ella);
     - número de una conexión manual (`channel_connections`): solo el secreto de esa conexión;
     - número que no es de nadie del negocio, o evento sin número: la conexión de la WABA (`waba_accounts`);
     - Messenger e Instagram: la conexión de la página (`fb_pages`) o de la cuenta (`ig_accounts`).

     Solo conexiones vivas (sin `deleted_at`) y en `ESTADOS_CON_CREDENCIAL`;
   - el secreto de cada candidata es su `app_access` guardado, **sea cual sea su origen**: una BYOK tomada por
     Embedded Signup conserva el de su app (ADR-0101);
   - todo se lee dentro del `tenant_scope` del negocio y con `tenant_id` explícito.
2. **Qué autoriza cada firma** (`webhooks.py::_firmantes_del_post`, `Firmantes`):
   - la de la **plataforma** autoriza cualquier grupo, como hasta hoy (nadie más la conoce);
   - la de una **conexión** autoriza solo los grupos de los que es dueña. Por el slug solo cuentan los grupos de su
     negocio;
   - sin ningún grupo autorizado: 401. Sin ningún secreto contra el que verificar: fail-closed (HU-ARCH-05), con
     el escape de test intacto. Un error al leer cuenta como "hay secreto", nunca habilita el escape.
3. **Fail-closed por el slug.** El slug ya no atribuye nada: un grupo sin negocio resuelto queda en
   `webhook_events` como `unresolved_tenant` y no se procesa, aunque lo autorice la plataforma. El de otro negocio
   sigue siendo `tenant_mismatch`. El slug y la facturación se miran como antes (la facturación, después de la
   firma).
4. **Handshake por slug** (`webhook_secrets.verify_tokens_aceptados`): acepta el token que registró cada conexión
   del negocio: el de la manual primaria, el propio de cada conexión viva y el global para cada BYOK sin token
   propio (es el que registran el wizard y "Registrar webhook"). Sin ninguno, el global, como antes. Se comparan
   todos en tiempo constante.
5. **Registrar el webhook de una conexión** (`meta_health_service.register_connection_webhook`, ruta
   `POST /api/v1/admin/meta/connections/{id}/webhook/register`):
   - **BYOK:** en la app de esa conexión, con su App Secret, la URL por slug del negocio y su `verify_token` (el
     propio o el global). Sin secreto guardado, error `missing_app_secret`: nunca cae a la app de la plataforma.
     Dueño del negocio o master;
   - **app de la plataforma** (`meta_credentials.uses_platform_app`: origen distinto de `byok`, o el `meta_app_id`
     de la plataforma): **solo el master**, con la URL genérica y el token global, los mismos del alta de Embedded
     Signup. El dueño recibe 403 `platform_app_master_only`;
   - la ruta vieja `POST /webhooks/register` queda obsoleta: con `connection_id` hace lo mismo; con solo `channel`,
     registra la única conexión viva del canal y, si hay varias, pide elegir (`connection_required`).
6. **Listado** (`GET /webhooks`): una fila por conexión (`connected`, `degraded` o `error`) con `connection_id`,
   `origin`, `uses_platform_app` y `can_register`. Una BYOK tomada por Embedded Signup muestra la suscripción de la
   app de la plataforma, que es la que usa. A quien no es master no se le muestran la URL ni el último error de la
   app de la plataforma (criterio de HU-CONN-07).

7. **Excepciones a ADR-0094** (las dos en `core/gateway/meta_credentials.py`, que sigue siendo la única regla de
   dónde vive cada credencial):
   - **El webhook no usa `credentials_for`.** Ese resolver dice con qué app se *llama* a Graph según el origen
     vigente: para una BYOK tomada por Embedded Signup da la de la plataforma, y exige token de acceso aunque solo
     haga falta el secreto. El webhook necesita con qué secretos puede *venir firmada* la conexión:
     `stored_app_secret` (el `app_access` guardado, de cualquier origen, ADR-0101) y `stored_verify_token`.
     "Registrar webhook" usa `app_credentials_for` (app y secreto según el origen, sin token de acceso), que
     comparte la regla de `credentials_for`.
   - **`uses_platform_app` mira el `meta_app_id`** (contra ADR-0094 §1 y §3, que no deducen nada de él): una
     "BYOK" con el `META_APP_ID` de la plataforma cuenta como de la plataforma. El wizard BYOK no lo impide, y sin
     esta guarda su dueño movería el callback de la app compartida a su slug y cortaría el genérico a todos.
     Rechazarlo en el wizard queda como seguimiento; con eso la guarda puede salir.
8. **Lo que se pospone a propósito:**
   - alinear `meta_app_id` tras una toma por Embedded Signup (ADR-0094 lo asignaba a esta HU): es el `subject_id`
     con el que se guardó el secreto de la app vieja, que el webhook sigue aceptando (ADR-0101). Alinearlo obliga a
     mover ese secreto o dejar de aceptarlo; queda en el BACKLOG;
   - revisar los `subscribed_apps` de la WABA al reclamarla (pendiente de ADR-0122): con este ADR la conexión
     eliminada ya no presta su secreto, así que sus entregas se rechazan; lo que queda es que el dueño de esa app
     siga recibiendo de Meta, que no pasa por nuestro webhook. Queda en el BACKLOG;
   - `meta_preflight.resolve_app_credentials` (preflight y checklist viejos) sigue eligiendo una conexión por
     negocio.
9. **Defensas agregadas en la revisión** (`security-reviewer`, `auditor-multitenant` y `arquitecto`, 2026-10-09;
   sin hallazgos altos de aislamiento entre negocios):
   - **El número manda sobre la WABA.** Con número registrado, la conexión de la WABA del payload solo cuenta si
     es la WABA de ese número; un número de una conexión manual solo acepta el secreto de la manual. Sin esto, el
     secreto de la app A autorizaba, dentro del negocio, la WABA de A combinada con el número de B;
   - **rechazo previo** (`_rechazo_previo`): 401 sin rutear ni leer secretos si el header no es
     `sha256=<64 hex>` (salvo el escape de test) o si el POST trae más de `MAX_GRUPOS_SIN_PLATAFORMA` (100) grupos
     y no lo firmó la plataforma. Los secretos se memorizan por conexión dentro del POST;
   - por el slug, si ningún grupo del negocio tenía secreto candidato, el 401 se loguea como
     `webhook_grupo_sin_conexion` y no suma al contador de la alerta de spoofing (es compartido por todos los
     slugs). Sigue siendo 401 para que Meta reintente y el evento entre cuando el número se sincronice;
   - las filas `tenant_conflict`, `tenant_mismatch` y `unresolved_tenant` guardan `signature_ok` según la firma
     del grupo, no `True` fijo;
   - `meta_token_store.get_token` filtra `tenant_id` en el `WHERE` (antes solo lo usaba para el AAD);
   - registrar exige una conexión `connected`, `degraded` o `error` (`invalid_state` en las demás);
   - el listado no muestra la URL ni el error de una app BYOK cuyo callback no es la URL de este negocio ni la
     genérica (app compartida con otro negocio).

No hay migración: las filas de números, WABAs y tokens ya tienen la conexión.

**Antes de desplegar** (test y producción), una consulta de datos:
- conexiones vivas (`meta_connections` en `ESTADOS_CON_CREDENCIAL`) sin ningún número o WABA vivo con su
  `connection_id`, y `channel_connections` de WhatsApp sin número;
- negocios solo con conexión manual que reciben eventos de plantillas o de cuenta por el slug (llegan sin número y
  se rutean por la WABA, que una conexión manual no tiene en `waba_accounts`: ahora dan 401);
- conexiones `byok` con el `meta_app_id` de la plataforma.
Si aparece algo, se corrigen los datos antes de desplegar.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Ruta `/webhooks/whatsapp/<slug>/<conexión>` | Obliga a re-registrar todas las apps BYOK existentes, y el genérico seguiría necesitando resolver por activo. El activo del payload ya identifica la conexión sin cambiar URLs |
| Que la WABA del payload aporte siempre el secreto de su conexión | El remitente elegiría el secreto combinando la WABA de una conexión con el número de otra |
| Probar todos los secretos del negocio para cada grupo | Es elegir el secreto por el cuerpo: el de una conexión autorizaría los números de otra, y un número sin conexión volvería a procesarse |
| Mantener que el slug atribuya los números desconocidos a su negocio | Sin conexión dueña no hay secreto que verifique ese grupo; el pedido de la HU es fail-closed |
| Alertar también cuando una conexión firma los números de otra del mismo negocio | Es una configuración cruzada del propio negocio, no una inyección: se registra y se loguea `webhook_firma_de_otra_conexion` |
| Dejar que el dueño re-registre la app de la plataforma "porque los valores son fijos" | La app es de todos los negocios; un error ahí corta el webhook de la plataforma entera (design.md de SPEC-08) |

## Consecuencias

**Positivas:**
- Un negocio con varias conexiones (BYOK + Embedded Signup, o varias apps propias) recibe de todas.
- El secreto de una conexión ya no autoriza los activos de otra, ni siquiera del mismo negocio.
- "Registrar webhook" de una BYOK toca solo su app; la de la plataforma, solo el master.

**Negativas / lo que aceptamos pagar:**
- Por el slug, un número o WABA que no está registrado en ninguna conexión del negocio da 401 (antes se procesaba).
  Afecta a los eventos de prueba del panel de Meta (traen IDs inventados) y a un número agregado en Meta y todavía
  no sincronizado en otra WABA. Los del mismo WABA siguen entrando (ruteo por WABA).
- Un evento firmado por la app de una conexión para un número que se pasó a otra conexión del negocio, sin que la
  WABA se moviera, se sigue aceptando (cuenta la conexión de la WABA). Si el número y la WABA se movieron y la app
  vieja sigue suscrita, sus entregas se rechazan; las de la app nueva entran.
- Más lecturas por POST no firmado por la plataforma: una consulta de conexiones y una lectura del token store por
  conexión candidata, por grupo. Un POST trae casi siempre un grupo.
- El alta de Embedded Signup sigue re-registrando la suscripción de la app de la plataforma con sus valores fijos
  (URL genérica y token global) desde el flujo del negocio. No cambia nada si ya estaba bien; queda como
  seguimiento moverlo al master.
- Messenger e Instagram usan la misma regla con la conexión de la página o de la cuenta. Hoy no hay clientes.
- A una BYOK sin `verify_token` propio se le registra el global, que el handshake de cualquier slug acepta para
  esas conexiones. Solo sirve para pasar un handshake (eco del challenge), ya pasaba con el wizard; generar uno
  propio por conexión queda como seguimiento.
- Dos negocios con la misma app BYOK: el que registra último lleva el callback a su slug y al otro le llegan sus
  eventos como `tenant_mismatch`. Ya pasaba con el wizard; queda en el BACKLOG.

**Qué haría que revisáramos esto:** filas `signature_connection_mismatch` en producción que vengan de
configuraciones legítimas, que Meta empiece a mandar qué app firmó, o pasar a una URL por conexión.

## Impacto en el código

- `core/gateway/webhook_secrets.py` (nuevo): `conexiones_del_grupo`, `secretos_del_grupo`, `verify_tokens_aceptados`.
- `core/gateway/meta_token_store.py`: `get_token` filtra `tenant_id`.
- `core/gateway/webhooks.py`: `Firmantes` por grupo, `_rechazo_previo`, `_firmantes_del_post(..., negocio_del_slug)`,
  `receive_whatsapp_tenant` (rutea antes de verificar), la puerta de `_process_payload` y el handshake por slug.
- `core/gateway/connections.py`: se retiran `resolve_own_app_secret` y `resolve_effective_app_secret`;
  `core/gateway/meta_byok.py`: se retira `resolve_byok_app_secret` y `ESTADOS_CON_CREDENCIAL` pasa a público.
- `core/gateway/meta_credentials.py`: `uses_platform_app`, `stored_app_secret`, `stored_verify_token`,
  `app_credentials_for` (y `credentials_for` usa `stored_app_secret`).
- `core/admin/meta_health_service.py`: `register_connection_webhook` (con `app_credentials_for`), `verify_token_de_registro`,
  `WebhookRegistroError`, `register_channel_subscription` (ruta vieja) y `webhook_subscriptions_status`.
- `core/api/routers/meta_ops.py`: ruta por conexión, la vieja obsoleta y los campos nuevos del listado.
- Tests: `tests/integration/test_webhook_por_conexion.py` (nuevo, `aislamiento`), `tests/test_webhook_byok_signature.py`,
  `tests/integration/test_webhook_slug.py` y los que asumían un secreto por negocio.
