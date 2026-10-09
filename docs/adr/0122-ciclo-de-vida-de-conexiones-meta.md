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
