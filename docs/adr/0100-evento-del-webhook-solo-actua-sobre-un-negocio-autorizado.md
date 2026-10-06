# ADR-0100 — Un evento del webhook solo actúa sobre un negocio autorizado por su firma

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Enmienda:**
  - ADR-0040, decisión 4 ("el genérico solo conoce el `.env`"), su consecuencia "el `tenant_id` de resolución siempre
    es explícito (nunca del cuerpo)" y la alternativa rechazada "probar todas las variantes de firma". En el genérico
    el cuerpo solo elige **qué secretos probar**, y cada secreto autoriza únicamente a su dueño (el de la plataforma, a
    todos). El arreglo del 2026-09-29 ya se había apartado de ADR-0040 sin ADR (probaba los secretos de todos los
    negocios del payload); este ADR lo registra y lo corrige.
  - ADR-0031, Decisión: el negocio de un evento de plantilla ya no lo decide `resolve_tenant_for_message_template`.
    Es el de la entry del webhook; el resolvedor solo comprueba que coincidan.
  - ADR-0032, decisión 4: el estado de un mensaje se busca dentro del negocio de la entry.
    `resolve_tenant_for_message` ya no decide nada: solo distingue en el log "es de otro negocio" de "no existe".
- **Contexto:** hallazgo alto preexistente del `arquitecto` al revisar ADR-0095; relacionado con ADR-0028
  (`webhook_events`) y ADR-0095 (ruteo por número).

## Contexto

`POST /webhooks/whatsapp` recibe eventos de varios negocios en un mismo POST (todos los suscritos a la misma app de
Meta). Desde el arreglo del 2026-09-29 (BYOK firmaba con su app y todo daba 401), juntaba en una lista el App Secret
de cada negocio del payload más el de la plataforma, aceptaba la firma con **cualquiera** y procesaba **todas** las
entries. Un negocio con app propia conoce su secreto: firmaba un POST con una entry suya y otra con el número de A, y
le inyectaba mensajes a A, que su agente respondía con las credenciales de A al número que eligiera el atacante.

Además, los eventos de plantillas (`message_template_*`) y de estado (`statuses`) resolvían el negocio por el ID de la
plantilla o del mensaje y escribían en él sin compararlo con el negocio de la entry: con una firma válida propia, un
negocio cambiaba el estado de una plantilla ajena o marcaba como fallidos los mensajes de otro, también por el
endpoint por slug.

## Decisión

Cada grupo de eventos `(entry, número)` se procesa solo si lo firmó la plataforma o su propio negocio, y ningún evento
escribe en un negocio distinto del de su grupo.

1. **Quién firmó** (`core/gateway/webhooks.py::_firmantes_del_post`). Se rutea el POST una sola vez
   (`_rutear_grupos`, con `route_event` de ADR-0095) y se verifica la firma:
   - con el secreto de la **plataforma** (`connections.platform_app_secret`: `WHATSAPP_APP_SECRET`, o
     `META_APP_SECRET` si el primero está vacío, el mismo criterio de antes en un solo lugar): autoriza a cualquier
     negocio. Ningún negocio lo conoce, y es con el que Meta firma las conexiones BYOK que tomó el Embedded Signup;
   - si no, con el secreto **propio** de cada negocio de los grupos (`connections.resolve_own_app_secret`: conexión
     manual → BYOK, sin caer al `.env`): autoriza solo a ese negocio. Un grupo en conflicto no aporta candidato.

   Sin plataforma ni negocio que verifique: 401, como hasta ahora. Sin ningún secreto contra el que verificar:
   fail-closed (HU-ARCH-05), con el escape de test intacto; un error al leer el secreto de un negocio cuenta como
   "tiene secreto", así nunca habilita ese escape. Si el genérico queda autorizado por un negocio y no por la
   plataforma, se registra `webhook_generico_firmado_por_negocio` (info): mide cuántas suscripciones BYOK siguen
   apuntando al genérico, para poder retirar esa rama.
2. **La puerta por grupo** (`_process_payload`, ahora con `firmantes` obligatorio). Tras resolver el negocio (conflicto
   → mismatch del slug → sin resolver), un grupo de un negocio que no firmó no se despacha:
   - queda en `webhook_events` con `process_error="signature_tenant_mismatch"`, sin negocio y con
     `signature_ok=False`. Ese `False` no significa que el HMAC fuera inválido (eso da 401 y no deja filas), sino que
     el firmante válido no autoriza a ese negocio. Es el primer uso de la columna (ADR-0028);
   - se loguea `webhook_firma_no_es_del_negocio` (solo IDs) y se manda una alerta crítica, una vez cada 24 h por par
     (firmantes, negocios afectados), con `SET NX EX` en Redis;
   - va antes del chequeo de facturación: ese chequeo registra la fila a nombre del negocio
     (`tenant_not_operational`), y un evento que A no mandó no debe quedar atribuido a A.

   La respuesta sigue siendo 200 sin contar los rechazos: un 5xx haría que Meta reintentara en vano. El endpoint por
   slug pasa su propio negocio (`Firmantes.de_negocio`) y no cambia.
3. **Plantillas y estados** reciben `expected_tenant_id`, obligatorio: el negocio del grupo. Así no se escribe la
   fila, ni un `MessageCost`, ni se manda la alerta de "plantilla rechazada" de otro negocio.
   - Plantillas (`meta_template_webhooks.handle_template_*`): `meta_template_id` es único en toda la tabla, así que
     el resolvedor `SECURITY DEFINER` es exacto. Si da otro negocio, el evento se descarta con un aviso; si no, el
     scope y las consultas usan el negocio esperado.
   - Estados (`meta_message_status.apply_status_update`): el wamid solo es único por negocio y
     `resolve_tenant_for_message` hace `LIMIT 1`, así que el mensaje se busca dentro del scope del negocio esperado
     (wamid + `tenant_id`). Si no está ahí, el evento se descarta, y el resolvedor solo se consulta para el log:
     `status_update_de_otro_negocio` (warning) o `status_update_sin_fila_local` (info).
4. **Marcar procesado** filtra por el negocio del grupo: un `external_id` repetido por B no marca la fila de A. Un
   payload que no es un objeto JSON se ignora con 200 en vez de reventar.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| 401 para todo el POST si algún grupo no está autorizado | Un POST legítimo de la app de B perdería sus propios eventos y Meta lo reintentaría en vano |
| Validar el genérico solo con el secreto de la plataforma | Vuelve el bug del 2026-09-29: las entregas BYOK al genérico dan 401 |
| Obligar ya a los negocios BYOK a usar solo el endpoint por slug | El wizard BYOK ya registra la URL por slug (`meta_byok.py`), pero las suscripciones anteriores pueden apuntar al genérico y habría que re-registrarlas (`upsert_app_subscription`) antes de cortar. Queda como seguimiento, medido con `webhook_generico_firmado_por_negocio` |
| Que el secreto de la plataforma solo autorice a negocios sin app propia | Rompe las conexiones BYOK tomadas por Embedded Signup hasta HU-CONN-05, sin cerrar ningún hueco (nadie más conoce ese secreto) |
| Decidir el negocio de un estado con `resolve_tenant_for_message` | `LIMIT 1` sobre un wamid que solo es único por negocio: si dos negocios lo tuvieran, descartaría el estado legítimo de uno |
| Registrar el grupo rechazado con el `tenant_id` del negocio que no firmó | Le atribuiría a A un evento que A no mandó; se registra sin negocio, como `tenant_mismatch` y `tenant_conflict` |

## Consecuencias

**Positivas:**
- Un negocio ya no puede inyectar mensajes en otro, ni cambiar sus plantillas o el estado de sus mensajes, con su
  propio secreto, por ninguno de los dos endpoints.
- La firma y el despacho usan la misma resolución (ADR-0095), una sola vez por POST.
- El cron de "worker caído" (`count_stalled_webhook_events`) deja de contar las filas con `process_error`: nunca se
  marcan procesadas. Ya alertaba cada hora, para siempre, por las filas `unresolved_tenant`, `tenant_conflict`,
  `tenant_mismatch` y `tenant_not_operational`, y este ADR agrega un rechazo que cualquier negocio puede provocar.

**Negativas / lo que aceptamos pagar:**
- Dos negocios con la misma app (el mismo App Secret) se autorizan entre sí: no se puede distinguir con criptografía, y
  quien tiene el secreto de A ya puede hacerse pasar por su app ante Meta. Se registra `webhook_secreto_compartido`.
- Un negocio con varias conexiones solo autoriza con su secreto efectivo (la conexión manual; si no, la conexión Meta
  viva más reciente, de cualquier origen), como hasta hoy: una entrega firmada por otra de sus apps sigue dando 401.
  HU-CONN-05 debería dar el secreto de la conexión dueña del número o la WABA del grupo (`_secretos_propios` ya
  devuelve una tupla).
- La WABA de A suscrita a la app BYOK de B que apunta al genérico se procesaba; ahora se rechaza y alerta.
- Quien conozca el secreto de la plataforma autoriza a cualquier negocio. Si es el de la app de un cliente concreto
  (por ejemplo, un `WHATSAPP_APP_SECRET` heredado del monolito), ese cliente puede firmar "como plataforma": hay que
  confirmarlo antes de desplegar. Era igual antes, pero ahora es la única llave que autoriza a todos.
- Una fila rechazada puede ocupar de antemano el `external_id` de un evento legítimo en `webhook_events`
  (`ON CONFLICT DO NOTHING`; en plantillas es un hash del `value`). Solo se pierde la fila de auditoría, no el
  procesamiento; ya pasaba con los otros motivos de rechazo.
- Messenger e Instagram usan la misma regla con el secreto propio de WhatsApp del negocio (`resolve_own_app_secret`):
  autoriza solo sus propios grupos, nunca los de otro. Hoy no hay clientes.

**Qué haría que revisáramos esto:** HU-CONN-05 (un secreto por conexión), filas `signature_tenant_mismatch` en
producción que vengan de configuraciones legítimas, que `webhook_generico_firmado_por_negocio` llegue a cero (se
puede retirar la rama de negocios del genérico) o que Meta empiece a mandar en el POST qué app lo firmó.

## Impacto en el código

- `core/gateway/connections.py`: `platform_app_secret`, `resolve_own_app_secret`; `resolve_effective_app_secret` queda
  igual (`own or platform`).
- `core/gateway/webhooks.py`: `_Grupo`, `_rutear_grupos`, `Firmantes`, `_firmantes_del_post` (reemplaza a
  `_tenant_y_secretos`), `receive_whatsapp`, la puerta en `_process_payload`, `signature_ok` en `_log_webhook_event`,
  `_alert_on_firma_ajena`, el filtro de `count_stalled_webhook_events`, `_mark_entry_processed` por negocio y los
  dispatchers con el negocio del grupo.
- `core/gateway/meta_template_webhooks.py`, `core/gateway/meta_message_status.py`: `expected_tenant_id`.
- Tests: `tests/test_webhook_byok_signature.py` (reescrito), `tests/integration/test_webhook_firma_por_negocio.py`
  (nuevo) y los llamadores de los handlers.
