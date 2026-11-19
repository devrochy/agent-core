# ADR-0122 — Ciclo de vida seguro de las conexiones de Meta: pausa real y borrado lógico

- **Estado:** aceptado
- **Fecha:** 2026-10-08
- **Supersede/enmienda:**
  - enmienda ADR-0038 (desactivar y eliminar);
  - enmienda ADR-0095: `revoked` sigue siendo dueña del número, pero ahora sí pausa;
  - enmienda ADR-0101 §1/§3: `revoked` vuelve a prestar el secreto, solo para la entrada y siempre al final;
  - enmienda ADR-0102: "Activar" ya no pasa a `connected` sin chequear;
  - enmienda ADR-0108: un aviso con la conexión pausada no se reintenta.
- **Contexto:** HU-CONN-03 (defectos 5 y 6 de la auditoría de Conexiones y "acciones sobre borradas"), issue
  agent-core #336. Revisado por el agente `arquitecto` antes de implementar ("procede con cambios"; sus diez cambios
  están incorporados).

## Contexto

Al auditar el código (2026-10-08):

1. **Desactivar no pausa.** `revoked` solo cortaba la firma BYOK (ADR-0101). Una conexión de Embedded Signup o
   manual desactivada seguía recibiendo, la IA contestaba y el sender enviaba por ella: ni el ruteo, ni el consumer,
   ni `resolve_credentials` miraban el estado. Una BYOK desactivada daba 401: el mensaje no se guardaba y Meta
   reintentaba.
2. **Eliminar borra historia.** `soft_delete_connection` marcaba la conexión, pero hacía `DELETE` físico de
   `phone_numbers` y `waba_accounts`, y por `ON DELETE CASCADE` se iban `message_templates` y `pricing_daily`
   (invariante 8). `rollback_failed_byok` hacía lo mismo. Los tokens quedaban vigentes.
3. **Activar no verifica.** Pasaba cualquier estado salvo `draft` a `connected`, incluso una conexión ya eliminada.
4. **Acciones sobre borradas.** `get_owned` no filtra `deleted_at`: facturación, diagnóstico, activar y volver a
   eliminar operaban sobre una conexión eliminada.
5. **Respaldo silencioso.** Con la conexión de la conversación borrada, sin número o sin token, `resolve_credentials`
   caía a la conexión primaria o al `.env`: el cliente recibía la respuesta desde **otro número**.

## Decisión

### 1. Conexión pausada

`core/gateway/meta_connection_state.py`: una conexión está **pausada** si tiene `deleted_at`, está en `revoked` o
en `draft` (`motivo_pausa`). `pending`, `connected`, `degraded` y `error` siguen enviando (ADR-0101).

### 2. Pausa real

- **Entrada.** El consumer guarda el mensaje siempre. Si la ruta de la conversación es una conexión pausada, **no
  corre la IA** (ni horario ni keywords, que también responden) y avisa a los dueños con un push, **una vez al día
  por conexión** (llave `conexion_pausada:aviso:<tenant>:<conexión>` en Redis). Si Redis no responde, no se manda:
  sin la llave sería un push por mensaje. El debounce revisa lo mismo antes del LLM: un turno agendado antes de
  pausar no gasta el modelo ni escala.
- **Firma de una BYOK pausada.** `_ESTADOS_CON_CREDENCIAL` suma `revoked`, y `_resolve_byok_token` ordena primero
  por "viva antes que pausada" y **después** por origen. Así una pausada nunca le gana a una viva, tampoco a una
  BYOK que tomó Embedded Signup y conserva el secreto de su app. Alimenta la firma y el `verify_token` de la
  entrada; las eliminadas siguen fuera.
- **Salida.** `send_to_conversation` y `send_template_to_conversation` dejan el mensaje `failed` con el motivo y
  devuelven `False` sin encolar. Los envíos humanos de la API responden 409 `connection_paused` antes de intentar.
  Lo encolado antes de pausar falla en `outbound_send` con `ConexionPausadaError`: permanente, **sin escalar**.
  Pausar un número con 40 conversaciones en cola no genera 40 escalaciones.
- **Avisos por evento** (enmienda a ADR-0108). Con la conexión pausada no se lanza `NoticeNotSentError`. El outbox
  lo reintentaría 5 veces en unos 18 minutos, dejaría 5 mensajes fallidos y lo perdería igual. Queda un solo
  `failed` con el motivo y el id estable del aviso (`modo = "conexion_pausada"`).

### 3. Sin respaldo silencioso

Si la conversación tiene `meta_connection_id` y esa conexión no da credenciales (pausada, sin número vivo o sin
token), `resolve_credentials` devuelve `("meta", {})`. La primaria y el `.env` quedan solo para conversaciones sin
ruta propia (número de la plataforma). En `outbound_send`, una conexión viva sin credenciales falla en el acto, sin
los tres reintentos, y se escala: eso sí lo tiene que atender una persona.

### 4. Activar con criterio (enmienda a ADR-0102)

"Activar" aplica solo a una conexión **pausada y viva**:

1. refresca la suscripción de la app BYOK, porque el cron de 15 min no revisa las de `revoked`;
2. corre el diagnóstico en el request, acotado a 60 s;
3. aplica `next_status` tratando la pausada como `error`: solo mejora con el diagnóstico verificado (token
   verificado y, en BYOK, la app y la WABA suscritas);
4. si queda `connected` o `degraded`, la reactiva releyendo la fila con `FOR UPDATE` (sigue `revoked`, viva y
   `misma_conexion`).

Si no, devuelve el check que falla con su detalle. Para `draft` y `pending` el alta se termina con el asistente.
`degraded` y `error` se recuperan solas con el diagnóstico. Una conexión de Embedded Signup o manual sin credenciales
en el token store (`ok=None`) ya no sale con "Activar": se reautoriza.

### 5. Acciones solo sobre conexiones vivas

`get_live_meta_connection` = `get_owned` + `deleted_at is null` (con `FOR UPDATE` opcional). Lo usan activar,
desactivar, eliminar, facturación, diagnóstico, créditos y `resolve_waba_and_credentials` (números, plantillas,
resuscribir). `get_owned` no cambia: se usa con modelos sin `deleted_at`. Las consultas que buscan conexiones
`connected` sin mirar `deleted_at` suman el filtro: wizard, suscripciones, créditos, precios y plantillas.

### 6. Eliminar sin perder historia

- **Migración 0063.**
  - `phone_numbers` y `waba_accounts` suman `deleted_at`, con backfill desde sus conexiones ya eliminadas (exige
    `BYPASSRLS`, ADR-0094 §5) y `lock_timeout`.
  - El único global de `phone_numbers.phone_number_id` pasa a **parcial** (solo filas vivas) más un único
    **completo** `(tenant_id, phone_number_id)`. El completo obliga a revivir la fila, así que un camino que se
    olvide falla en vez de duplicar.
  - `uq_message_templates_meta_id` pasa de global a `(tenant_id, meta_template_id)`.
  - Las funciones de ruteo y de reclamo ignoran filas eliminadas.
  - Es irreversible.
- **`soft_delete_connection`** marca `deleted_at` en la conexión, sus números y sus WABAs, sin `DELETE`.
  Plantillas, `pricing_daily` y `message_costs` se conservan. `rollback_failed_byok` también pasa a borrado lógico,
  porque el intento pudo revivir una WABA con plantillas.
- **Revivir.** `waba_para_conexion` y `numero_para_conexion` (en `meta_asset_claims`, dentro de la transacción de
  `claim_assets`) se usan en los cinco caminos: BYOK, Embedded Signup, alta manual, sync de números y alta de
  números.
  - Reconectar el mismo activo en el mismo negocio revive la fila eliminada, así plantillas y precios siguen en su
    WABA.
  - Una WABA revivida queda `subscribed=False` hasta suscribirla (ADR-0102 §3).
  - Si la conexión anterior se queda sin números vivos, sus conversaciones pasan a la nueva. Si no, avisos y
    seguimientos fallarían cerrados.
- **Plantillas por negocio en los webhooks.** La entry ya viene ruteada a su negocio (ADR-0100): el estado de una
  plantilla se busca bajo ese `tenant_scope` y no con `resolve_tenant_for_message_template`. Esa función hacía
  `LIMIT 1` global y, con dos negocios con el mismo id, elegía uno al azar.
- **Revocación.**
  1. Primero se confirma el borrado, sin transacción abierta durante las llamadas a Graph.
  2. Se marcan `revoked_at` en todos los tokens de la conexión.
  3. En **Embedded Signup y manual** se desuscribe la app de la plataforma de la WABA
     (`DELETE /{waba_id}/subscribed_apps`, mejor esfuerzo). Se salta si otra conexión viva de la plataforma usa la
     misma WABA.
  4. En **BYOK** no se llama a Meta: la app es del negocio, y la firma de una conexión eliminada ya se rechaza.
  5. No hay `DELETE /me/permissions`: el token del system user del negocio puede servir a otras WABAs de su
     portafolio, incluso de otro negocio de la plataforma.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Liberar el número al desactivar (que `revoked` deje de ser dueña) | Otro negocio podría reclamarlo durante la pausa; ADR-0095 lo descartó |
| Escalar cada conversación que escribe a una conexión pausada | Al reactivar, el bot quedaría pausado en todas hasta resolverlas a mano |
| Que la cola escale los envíos fallidos por pausa | 40 conversaciones en cola darían 40 escalaciones y 40 pushes |
| Exigir la revocación en Meta para eliminar | Una conexión con el token ya muerto no se podría borrar nunca |
| `DELETE /me/permissions` (Embedded Signup) o desuscribir en BYOK | Puede romper otras WABAs del mismo system user; la app BYOK es del negocio |
| Filtrar `deleted_at` en `get_owned` | Se usa con modelos sin `deleted_at` (wizard, plantillas, packs) |
| Crear filas nuevas al reconectar en vez de revivir | Plantillas y precios quedarían en la WABA vieja |
| Hacer parcial `uq_waba_accounts_waba_connection` | Ya es `(waba_id, connection_id)` y revivir cambia el `connection_id` |
| "Activar" asíncrono con polling | Agrega un estado sin un segundo caso de uso; "Diagnosticar" ya corre en el request |

## Consecuencias

- Una conversación cuya conexión se pausó o eliminó **no responde** hasta reactivarla o hasta que el cliente
  escriba por otra ruta. Antes respondía desde otro número.
- Los mensajes que entran a un número pausado crean clientes y conversaciones que cuentan para la cuota.
- pack-commerce marca un seguimiento como hecho aunque el envío devuelva `False`: durante una pausa se consumen.
  Queda anotado.
- Un aviso por evento que no salió por la pausa queda `failed` con su id estable: al reactivar no se reenvía solo.
- El 0063 es irreversible. También elimina `resolve_tenant_for_message_template`, que ya no se usa.
- **Revisión `auditor-multitenant`:** sin hallazgos altos. Se corrigieron estos:
  - revivir un número revisa antes si otro negocio lo tomó, también por conexión manual;
  - el alta de Embedded Signup sobre una conexión que se eliminó a mitad de camino se aborta;
  - "Activar" no refresca la suscripción de la app de la plataforma;
  - no se desuscribe una WABA que otro negocio reclamó.

  **Queda pendiente:** al eliminar una BYOK, su app sigue suscrita a la WABA. Si otro negocio la reclama, la
  plataforma rechaza esos eventos, pero el dueño de la app podría redirigir su callback. Ya pasaba antes y se trata
  al reclamar la WABA, revisando sus `subscribed_apps` (HU-CONN-05).
- Tests: `tests/integration/test_conexion_ciclo_de_vida.py` (los cinco escenarios, revivir y reclamo por otro
  negocio). Además, se actualizaron los tests que exigían el comportamiento viejo (borrado físico, 401 de una BYOK
  pausada, Activar sin diagnóstico).

## Adenda 2026-10-19: la pausa también vale para las conexiones sin cuenta de Meta

**Contexto.** HU-AOS-23 (agent-core #472) encontró que pausar una conexión manual o de WhatsApp Web
(`channel_connections.active = false`) no la detenía: entraba el mensaje, corría la IA y salía la respuesta. ADR-0115
§2 ya decía que el ciclo de vida de una conexión sin cuenta (manual, `waweb`, correo, chat web) es su `active` /
`deleted_at`, pero la guarda de §2 solo miraba la cuenta de Meta (`cuenta_meta_de_conversacion`): para esas filas
no había cuenta y no había pausa. El consumer resuelve la conexión por `external_id` sin mirar `active`, y
`resolve_credentials` usaba las credenciales de la manual aunque estuviera desactivada.

**Decisión.**

1. **Guarda única ampliada** (`meta_connection_state.pausa_de_conversacion`, y `motivo_pausa_de_conversacion` que
   deriva de ella). Si la conversación tiene `connection_id` y esa fila no tiene cuenta de Meta, está pausada si
   `deleted_at` no es nulo («La conexión fue eliminada») o `active` es falso («La conexión está pausada»). Se
   evalúa antes de la salida por `channel == "web"`: el chat web también es una conexión, y su `active` es el
   «habilitado» del widget (el endpoint público ya responde `disabled`). La fila canónica de un número de Meta no
   tiene estado propio: su `active` no cuenta y manda la cuenta, como antes. Una fila ilegible (otro negocio) no
   pausa, como antes. Lee la fila una vez, con `tenant_id` explícito además de RLS.
2. **Entrada.** Igual que la pausa de Meta: el inbound **se guarda** (el dueño lo ve al reanudar y el historial
   queda completo), no corre la IA ni el horario ni las keywords, y queda `blocked_by = connection_paused` en
   `core_decisions`. El debounce revisa lo mismo antes del LLM. El push a los dueños, una vez al día por conexión,
   dice «Conexión pausada» (el tipo manual no es necesariamente WhatsApp; la cuenta de Meta sigue con «WhatsApp
   desactivado»), y el payload `core.connection_paused` suma `scope: "meta_account" | "connection"` para
   distinguir a qué id apunta `connectionId`.
3. **Salida.** Todos los caminos ya pasaban por la guarda, así que quedan cubiertos sin tocarlos: `send_to_conversation`
   (respuestas del turno y en vuelo por `kernel/egress`, crons de pack-commerce y pack-calendar, escalamiento y
   fuera de horario), `send_template_to_conversation`, `send_interactive_to_conversation`, la tarea `outbound_send`
   (lo encolado antes de pausar falla sin escalar), los avisos por evento (cobranza, suscripciones) y las respuestas
   del operador (409 `connection_paused`). El correo ya salía solo por una conexión activa
   (`get_active_connection`). `resolve_credentials` sigue sin mirar `active`/`deleted_at` de las filas sin cuenta:
   la defensa es la guarda, que corre antes en los cuatro llamadores.
4. **Sin filtrar `active` en el consumer.** Si el consumer ignorara la conexión pausada, la conversación quedaría
   sin ruta y `resolve_credentials` caería a la primaria o al `.env`: la respuesta saldría desde otro número, justo
   lo que prohíbe §3.
5. **Sin bandera.** Corrige una guarda que ya corre sin bandera en `politicas_de_entrada`; con todas las conexiones
   activas el resultado es idéntico. Revisado por el agente `arquitecto` («procede con cambios»): un interruptor
   global apagaría la pausa de todos los negocios para rescatar a uno. La vuelta atrás es revertir el cambio.
6. **Consola** (`/conexiones`): `pausable` también para las conexiones manuales y de WhatsApp Web
   (`pause_scope = "connection"`, endpoint `POST /admin/connections/{id}/action`, auditado como
   `connection.paused` / `connection.resumed`). El chat web se sigue pausando en Configuración → Chat web.

**Consecuencias.**

- **`choque_manual`.** Un negocio que pasó de una conexión manual a Embedded Signup con el mismo número y
  **desactivó** (sin eliminar) la manual vieja: esa fila gana la resolución (`conexion_canonica.py`) y ahora la
  conversación queda en silencio, con el push «Conexión pausada». Antes respondía. Es el comportamiento esperado
  (la fila está pausada), pero hay que detectarlo antes de desplegar: `backfill_conexiones(dry_run=True)` informa los
  choques por negocio, y la consulta del PR lista las conexiones sin cuenta `active = false AND deleted_at IS NULL`
  con entradas en los últimos 30 días. Corrección operativa: eliminar la manual obsoleta y correr
  `backfill-conexiones` del negocio. Seguimiento: que `asegurar_conexion_del_numero` religue una manual inactiva del
  mismo número en vez de devolver `choque_manual`.
- **Residual de WhatsApp Web.** El outbox `waweb:outbox:<tenant>` es por negocio, no por conexión: un job que el
  conector ya tomó y espera su delay anti-baneo (segundos) puede salir aunque la pausa llegue en ese intervalo. No
  se cubre.
- **Conversaciones viejas sin `connection_id`** (de antes de la conexión canónica, en un canal manual o `waweb`): la
  guarda solo ve el puente `meta_connection_id`, que ahí es nulo, así que un envío proactivo no queda cubierto hasta
  que entre el siguiente mensaje (el consumer les pone la conexión). Ya pasaba antes. Antes de desplegar, contar las
  conversaciones abiertas con `connection_id IS NULL` y canal distinto de `web`, y correr el backfill si hay.
- Si la pausa llega a mitad de un turno, `kernel/egress` ve el envío fallido y escala con `send_failed`, como ya
  pasaba con la cuenta de Meta.
- Revisado por `auditor-multitenant` y `security-reviewer`: sin hallazgos bloqueantes ni altos. Ningún cliente
  (APK, panel) consume todavía `core.connection_paused`, así que sumar `scope` no rompe nada.
- Tests: `tests/integration/test_pausa_conexion_propia.py` y `tests/test_pausa_de_conversacion.py`.

## Adenda 2026-10-19 (b): el chat web también se pausa desde la consola

Reemplaza la última frase del punto 6 de la adenda anterior («El chat web se sigue pausando en Configuración → Chat
web»). En test, el dueño pausó el WhatsApp por QR, escribió por el chat web y le respondió: la consola no dejaba
pausar el chat web y no decía que la pausa es solo de ese canal.

1. **Mismo endpoint, mismo alcance.** La fila `webchat` de la consola sale con `pause_scope = "connection"` y
   `pausable = true`. `POST /admin/connections/{id}/action` acepta `activate` / `deactivate` sobre ella y lo audita
   como las demás (`connection.resumed` / `connection.paused`). `primary` y `delete` siguen dando 404: la clave, los
   dominios y el alta del widget siguen en Configuración → Chat web. `webchat_settings` deja de emitirse y queda en
   el enum de la API para no romper los clientes generados.
2. **Un solo estado.** La pausa escribe el mismo interruptor que Configuración (`chat_web_conexion.establecer_habilitado`):
   `app_settings.web_chat_config.enabled` (fuente de verdad hasta el *contract*, ADR-0115) y `channel_connections.active`
   (lo que leen `webchat_resolve` y la guarda de §2) en una transacción. Primero toma `SELECT … FOR UPDATE` sobre
   `app_settings` (también `guardar_widget`, en el mismo orden) y después relee la conexión, para que un guardado
   concurrente no pise la pausa ni deje un estado previo viejo. El GET de Configuración sigue
   leyendo `app_settings`. Si `app_settings` no tiene el widget de esa conexión (otra clave o vacío), falla cerrado
   con 409 y no repara nada en silencio: lo corrige `backfill-conexiones`.
3. **Auditoría en los dos caminos.** Activar o desactivar desde Configuración (`PUT /settings/web-chat`) deja el mismo
   `connection.paused` / `connection.resumed` cuando el interruptor cambia (estado previo y nuevo leídos bajo el mismo
   candado); crear el widget no cuenta como reanudar. La auditoría va después del cambio, en su propia transacción,
   igual que en las demás conexiones.
4. **Permisos.** La consola exige `connections` en escritura; Configuración, `settings` en escritura. Un colaborador
   con uno y sin el otro pausa el chat web por un solo camino. Es la misma asimetría que ya tienen las conexiones
   manuales (se pausan solo desde la consola) y se acepta.
5. **Antes de desplegar**, comparar por negocio `app_settings.web_chat_config->>'enabled'` con el `active` de su
   conexión `webchat` viva; si alguno difiere, `backfill-conexiones` del negocio.

Tests: `tests/integration/test_pausa_chat_web_consola.py`.
