# ADR-0186 — WhatsApp vía Twilio con cuenta del negocio (BYOK) y autoconfiguración

- **Estado:** propuesto
- **Fecha:** 2026-11-07
- **Contexto:** D-64 (docs); HU-CONN-11 a HU-CONN-17 (SPEC-08). Primer uso: el agente comercial de solvershub con un número nuevo comprado en Twilio, mientras Meta verifica a solvershub.
- **Usa:** ADR-0052 (canales por entry points), ADR-0069 (catálogo de tipos de conexión), ADR-0072 (capacidades según la conexión), ADR-0094 (credenciales por conexión), ADR-0108 (plantillas por evento), ADR-0115 (conexión canónica), ADR-0122 (pausa y borrado lógico), ADR-0126 (webhook por conexión), ADR-0165 (salida estructurada).
- **Relacionado:** ADR-0187 (fase 2, Twilio como ISV con subcuentas).

## Contexto

`whatsapp_twilio` está en el catálogo desde la migración 0038 (`core/gateway/connection_types.py`) con `available=False`, sin esquema de credenciales y sin código: el panel lo muestra como «Próximamente». Todo lo que rodea a WhatsApp está atado a Meta:

- el webhook usa `inbound_adapter_for("meta")` y las funciones de ruteo filtran `provider = 'meta'`;
- la ventana de 24 h solo se refresca para `meta` (`core/orchestrator/consumer.py`);
- la media solo se descarga para `meta` (`media/ingesta.py`);
- `decide_retry` solo clasifica `GraphError`;
- plantillas, avisos por evento y segmentos exigen `meta_connection_id`;
- la unicidad del número entre negocios (`claim_assets`) solo corre para Meta.

Twilio difiere de Meta en tres puntos que obligan a decidir:

1. Los webhooks llegan en `application/x-www-form-urlencoded` y se firman con `X-Twilio-Signature`, un HMAC-SHA1 de la URL **pública** más los parámetros ordenados, hecho con el Auth Token de la cuenta.
2. Las plantillas son recursos de la Content API (`HX…`). Se mandan a aprobación aparte y Twilio no avisa por webhook cuando cambia su estado.
3. Los botones y las listas, aun dentro de la ventana de 24 h, se mandan como un Content ya creado: no hay un payload interactivo ad hoc.

Para que el negocio no tenga cuenta propia en Twilio (modelo ISV), Twilio exige que solvershub sea Meta Tech Provider con una app propia aprobada y una subcuenta por WABA. Hoy eso está bloqueado por la verificación de Meta; se decide aparte en ADR-0187.

## Decisión

**WhatsApp vía Twilio es un proveedor más del canal `whatsapp`, con la cuenta Twilio del negocio. El negocio solo entrega el Account SID y el Auth Token, y el backend configura el resto: descubre el número, registra los webhooks, sincroniza las plantillas y vigila la salud.**

1. **La conexión vive donde viven las demás.** Va en `channel_connections` con `provider='twilio'`, `type_id='whatsapp_twilio'`, `channel='whatsapp'` y `external_id` = el número del sender en E.164.
   - `{account_sid, auth_token}` van cifrados en `credentials_encrypted` (AES-GCM, `core/shared/crypto.py`).
   - Una tabla 1:1, `twilio_connections`, guarda lo propio de Twilio:
     - `public_id` aleatorio, único;
     - `account_sid`, `sender_sid` (`XE…`), `sender_e164` y `sender_status`;
     - `status` (`pending`, `connected`, `degraded`, `revoked`, `error`);
     - el display name y su estado;
     - las URL de webhook registradas y `previous_webhook`;
     - `subaccount_id` (NULL en esta fase);
     - `last_health_at` y `last_error_code`.
   - `meta_connection_id` queda NULL. No se generaliza `meta_connections`.
2. **El Auth Token es obligatorio.** Twilio firma los webhooks con el Auth Token, no con una API Key: sin él no se puede verificar la entrada. Una API Key para las llamadas REST queda como mejora, sin quitar el Auth Token.
3. **Alta en dos pasos y sin estado intermedio.**
   - `POST /api/v1/admin/connections/twilio/discover` valida las credenciales y lista los senders de WhatsApp con Senders API **v2** (`GET /v2/Channels/Senders?Channel=whatsapp`). Solo se pueden elegir los `ONLINE`.
   - `POST /api/v1/admin/connections/twilio` crea la conexión en este orden: reclama el número, cifra, crea, registra los webhooks del sender (`webhook.callback_url` y `status_callback_url` con POST), los relee para confirmarlos y sincroniza las plantillas.
   - Si el sender ya tenía webhooks, se guardan en `previous_webhook` y el alta exige `confirm_overwrite_webhook`. Al desconectar se pueden restaurar.
   - El alta es idempotente: el mismo sender en el mismo negocio devuelve la conexión existente.
4. **Un número, un negocio.** Se usa el mismo patrón que `phone_numbers`: un índice único parcial **global**, `uq_twilio_connections_sender_vivo`, sobre `twilio_connections(sender_e164) WHERE deleted_at IS NULL`.
   - La tabla tiene RLS forzado, pero la unicidad se aplica igual entre negocios.
   - Un segundo negocio con el mismo número recibe 409 `sender_ya_conectado`.
   - Dos altas simultáneas del mismo número se serializan con un candado `twilio_asset:sender:<e164>`, como `claim_assets`.
   - El borrado lógico libera el número.
5. **Webhook propio por conexión.**
   - Rutas públicas: `POST /webhooks/twilio/{public_id}/inbound` y `/status`.
   - El tenant sale solo de la ruta, a través de `twilio_route_connection(public_id)` (SECURITY DEFINER, mismo patrón que la migración 0063). Nunca sale del payload.
   - La firma se verifica contra la **URL exacta guardada al registrar el webhook**, no contra una reconstruida desde `X-Forwarded-*` detrás del proxy.
   - El `AccountSid` del payload debe coincidir con el de la conexión.
   - Responde 200 con `<Response/>`.
   - `InboundAdapter.verify_request` gana un kwarg opcional `url`, que los adaptadores existentes ignoran.
6. **Idempotencia en las mismas tres capas que Meta.**
   - Capa 1: tabla propia `twilio_webhook_events` con RLS, único `(conexión, kind, event_key)` (`event_key` es `MessageSid`, o `MessageSid:MessageStatus` para estados) y solo campos de diagnóstico.
   - Capa 2: `claim_message_once(tenant, "twilio", MessageSid)`.
   - Capa 3: el stream `agent_core:inbound` con su DLQ.
   - `webhook_events` (sin RLS, atada a `meta_app_id`) no se toca.
7. **El contrato de canal se mantiene.**
   - `parse_inbound` mapea:
     - `From` → `customer_external_id`. El prefijo `whatsapp:` se conserva también al enviar; Meta lo quita.
     - `To` → `connection_external_id`.
     - `Body` → `content` y `ProfileName` → `customer_name`.
     - `MediaUrlN` y `MediaContentTypeN` → `media`.
     - `ButtonPayload` o `ListId` → `interactive_id`.
   - Los status callbacks alimentan la misma máquina `queued < sent < delivered < read`. `sending` cuenta como `queued` y `undelivered` como `failed`, y un estado nunca retrocede.
   - La ventana de 24 h se refresca también para `twilio`.
   - La media se descarga con auth básica de la conexión.
8. **Plantillas en tabla propia, servidas por un resolvedor que no depende del proveedor.**
   - `twilio_content_templates` guarda:
     - `content_sid`, `friendly_name` (único vivo por conexión) y `whatsapp_name`;
     - `language`, `category`, `types` y `variables` (`{{1}}` contiguas, cada una con su ejemplo);
     - `approval_status` (`draft`, `creating`, `unsubmitted`, `received`, `pending`, `approved`, `rejected`, `paused`, `disabled`);
     - `rejection_reason`, `next_poll_at` y `deleted_at`.
   - Se crea con `POST /v1/Content` y se manda a aprobación con `POST /v1/Content/{sid}/ApprovalRequests/whatsapp`.
   - El estado se consulta por **polling** con backoff: 5 min durante la primera hora, 30 min hasta 48 h y luego cada hora hasta 14 días. Usa candado Redis, con el patrón de `acquire_sync_lock`.
   - Hay un sync horario completo.
   - Una plantilla enviada a aprobación no se edita: se crea una versión nueva.
   - `core/gateway/templates/resolver.py` devuelve `ApprovedTemplate{provider, ref, name, language, category, variables}` para Meta (envuelve el código actual sin cambiarlo) y para Twilio.
   - El resolvedor lo usan el selector de plantillas del chat y los avisos por evento (ADR-0108). Los envíos a segmentos (ADR-0150) lo adoptan después.
   - `OutboundAdapter` gana `send_content_template(credentials, to, content_sid, variables)` y el `send_template` de Meta no cambia.
   - Los valores de `resolve_values` se pasan en orden como `{"1": …, "2": …}`.
9. **Botones y listas desde v1, con Content cacheado.**
   - `send_interactive` de Twilio crea bajo demanda un Content `twilio/quick-reply` o `twilio/list-picker` y lo cachea en `twilio_interactive_contents`, con `(conexión, hash)` único.
   - El cuerpo, que genera el LLM, va como variable `{{1}}` y **no entra en el hash**. El hash cubre el tipo y los ids y títulos.
   - Si la creación falla, el envío cae a texto numerado.
   - Una purga borra en Twilio los Content sin uso.
   - `CAPACIDADES_POR_TIPO["whatsapp_twilio"]` declara botones, lista, plantilla aprobada y ventana de 24 h. Los límites de botones y filas de Twilio se fijan al implementar.
10. **Errores por categoría.** `ProviderError(ChannelError)` lleva `provider`, `code`, `http_status`, `category` y `retry_after`, y `GraphError` pasa a ser su subclase. `decide_retry` decide por categoría:

    | Categoría | Ejemplos de Twilio | Reintento |
    |---|---|---|
    | TRANSIENT | 5xx, timeout de conexión | sí, backoff con jitter |
    | RATE_LIMIT | 429, 20429 | sí, respetando `Retry-After` |
    | QUOTA | límite diario del sender | a la hora siguiente |
    | AMBIGUOUS | timeout de lectura | uno en conversación, ninguno en masivos |
    | AUTH | 401, 20003 | no; la conexión pasa a `revoked` |
    | CONFIG | sender OFFLINE o inexistente | no; la conexión pasa a `degraded` |
    | WINDOW | 63016 | no; se sugiere plantilla |
    | OPT_OUT | 21610 | no; se registra la baja |
    | RECIPIENT | número inválido | no |
    | CONTENT | plantilla inválida, rechazada o pausada | no |

    Un 4xx desconocido es permanente y un 5xx desconocido es transitorio. Los códigos exactos se fijan contra el diccionario de Twilio, con un test por tabla.
11. **Salud y costos.**
    - Un cron cada 15 min revisa la cuenta, el estado del sender y si los webhooks siguen apuntando a nosotros.
    - Al pasar a `degraded` o `revoked` avisa con `send_alert_once`.
    - El costo se lee del recurso Message (`price`) cuando el mensaje llega a su estado final, y va a `message_costs`. Si `price` solo trae la tarifa de Twilio y no el cargo de Meta, la UI lo dice.
12. **Apertura por negocio.** El tipo se abre con la bandera `settings.canales.whatsapp_twilio`, que solo cambia el master. El catálogo calcula `available` por negocio, y la bandera arranca encendida solo en el negocio solvershub.
13. **El código vive en un paquete de canal.** Va en `packages/channel-twilio` (entry point `twilio` en `agent_core.channels`), con el mismo patrón que `packages/channel-waweb`. El stub `packages/channel-whatsapp` documentado en `CLAUDE.md` no se reutiliza.

**Autorización**
- `require_owner_or_master`: alta, rotación de credenciales, desconexión y mensaje de prueba.
- `require("connections", write)`: plantillas.
- `require("connections", read)`: lecturas.
- Revelar credenciales usa el endpoint existente con step-up, y el Auth Token nunca sale en un listado.
- Todas las tablas nuevas llevan `tenant_id`, RLS forzado y policy de aislamiento (invariante 1).
- El ruteo del webhook lee sin contexto de tenant solo a través de la función SECURITY DEFINER `twilio_route_connection`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Generalizar `meta_connections` para Twilio | Está atada a la app, la WABA y los tokens de Meta, y su salud y su producción son de Meta. Una fila falsa contamina la salud y los estados |
| Generalizar `message_templates` | Tiene un CHECK de estados de Meta, cuelga de una WABA y es una tabla caliente; el riesgo de migración no se justifica. Un resolvedor común da el mismo resultado a los consumidores |
| Generalizar `webhook_events` | No tiene RLS, `meta_app_id` es NOT NULL y hay un test que restringe quién la escribe |
| Reconstruir la URL de la firma desde `X-Forwarded-*` | Frágil detrás del proxy de Coolify. La URL registrada es determinista |
| Solo API Key, sin Auth Token | No se podrían verificar las firmas |
| Tabla de reclamos sin RLS (`twilio_sender_claims`) | Viola la invariante 1, y un índice único parcial global sobre la tabla con RLS logra lo mismo, como ya hace `phone_numbers` |
| Pedir al negocio que pegue las URL en la consola de Twilio | Más gestión para el negocio, y no detecta cuando alguien las cambia |
| v1 sin botones ni listas (texto numerado) | El dueño los pidió desde v1, y el caché por estructura evita crear un Content por mensaje |
| Twilio como ISV con subcuentas desde ya | Exige que solvershub sea Meta Tech Provider, hoy bloqueado (ADR-0187) |
| Sobrecargar `send_template` con kwargs de Twilio | La firma ambigua invita a errores en el camino de Meta |

## Consecuencias

**Positivas**
- solvershub tiene un canal oficial de WhatsApp mientras Meta lo verifica.
- Para el negocio, conectar es pegar dos datos y elegir un número.
- Cada error de proveedor queda clasificado: deja de caer en la DLQ como «transitorio».
- El resolvedor de plantillas prepara el camino para otros proveedores.

**Negativas / lo que aceptamos pagar**
- Cuatro tablas nuevas, con sus migraciones: `twilio_connections`, `twilio_webhook_events`, `twilio_content_templates` y `twilio_interactive_contents`.
- Un polling de plantillas, porque Twilio no avisa los cambios de estado.
- Un timeout de lectura al enviar puede duplicar un mensaje: se mitiga con la regla AMBIGUOUS.
- Si el negocio rota el Auth Token en Twilio, firma y envío fallan hasta el siguiente ciclo de salud (15 min) o hasta que rote las credenciales en el panel.
- Mientras Meta no verifique al negocio (incluido solvershub), Twilio limita a 2 senders. Si Meta rechaza el display name, el número queda en 250 mensajes iniciados por el negocio cada 24 h.
- En v1 no se impide que un número esté a la vez en Meta directo y en Twilio.

**Qué haría que revisáramos esto**
- Que solvershub quede verificado como Meta Tech Provider: si el Embedded Signup directo de Meta (ADR-0128) cubre lo mismo, Twilio deja de ser necesario y la fase ISV (ADR-0187) se reconsidera.
- Que Twilio publique webhooks de estado de plantillas: sobra el polling.
- Que más del 5 % de los envíos interactivos caiga a texto por errores de creación de Content.

## Impacto en el código

**Backend**
- `core/gateway/channels/base.py`: `ProviderError`, `url` en `verify_request` y `send_content_template`.
- `core/tasks/outbound_send.py`: `decide_retry` y la validación de credenciales.
- `core/gateway/connection_types.py`: esquema, `external_id_key`, disponibilidad por bandera y capacidades.
- `core/gateway/sender.py`: `resolve_credentials` y `send_template_to_conversation`.
- Nuevos `core/gateway/twilio_webhooks.py`, `core/gateway/twilio/` y `core/gateway/templates/resolver.py`.
- `core/gateway/event_notices.py`, `core/orchestrator/consumer.py` y `media/ingesta.py`.
- `core/gateway/avisos_canal.py` y `avisos_opciones.py`: se quita la exclusión de Twilio.
- Nuevo `core/api/routers/twilio_connections.py`.
- `core/tasks/celery_app.py`: salud, polling y sync.
- Nuevo `packages/channel-twilio/` y migraciones en `alembic/versions/`.

**Panel**
- Alta guiada.
- Ficha `conexiones/twilio/[connectionId]` con Resumen y salud, Credenciales, Plantillas y Prueba.
- Selector de plantillas con `{provider, template_ref}`.

**Tests que cambian**
- `tests/integration/test_conexiones_selector_canal.py`: sigue con 2 «Próximamente» con la bandera apagada, y se agrega el caso con la bandera encendida.
- `tests/test_capacidades_canal.py`, `tests/test_opciones_respuesta_texto.py` y `tests/test_avisos_opciones_unidad.py`.
- El test de escritura de `webhook_events` se amplía a `twilio_webhook_events`.

**PRs, en orden**

| PR | Contenido |
|---|---|
| PR-1 | Errores por categoría y contrato de canal |
| PR-2 | Paquete `channel-twilio` |
| PR-3 | Conexión, unicidad del número y alta |
| PR-4 | Webhooks, ventana, media y estados |
| PR-5 | Salud, acciones y costos |
| PR-6 | Plantillas |
| PR-7a | Resolvedor, avisos por evento y selector del chat |
| PR-7i | Botones y listas |
| PR-7b | Segmentos (después de la fase 1) |

## Adenda (2026-11-08) · lo que cambió al implementar (PR-2 a PR-7b)

La fase 1 quedó implementada en PRs apilados (#714, #716, #717, #718, #719, #723, #728 y #729; migraciones 0121 a 0124). Esto corrige lo escrito arriba donde el código terminó distinto. El ADR sigue **propuesto** hasta probarlo en test con el número real.

### Cambios de decisión

- **§9, sin texto de respaldo (cambia lo aprobado).** El ADR decía «si la creación falla, el envío cae a texto numerado». No se implementó: un interactivo que Twilio no acepta falla como error de Twilio y la conversación escala, igual que en Meta. El outbox no sabe si quien encoló el mensaje acepta un texto en lugar del botón (`sin_degradar` no viaja con el mensaje), y un texto no confirma una confirmación ni ejecuta una opción directa. Degradar a texto se decide antes de encolar, en `send_interactive_to_conversation`. Se revisa si en test Twilio rechaza los interactivos (ver «Pendiente de verificar»); agregarlo exige guardar en el mensaje si acepta degradarse.
- **§9, un Content por forma, no por hash.** La documentación de Twilio admite variables en el cuerpo, los títulos y los ids de un mensaje en sesión, así que no hace falta un Content por cada combinación de botones.
  - La clave de `twilio_interactive_contents` es `(tenant, conexión, forma)`, con forma `quick_reply:N` o `list:N`.
  - Todo lo variable viaja en `ContentVariables`. Los ejemplos del Content son neutros, para no dejar datos de un cliente en la cuenta.
  - La fila guarda la `account_sid` que la creó: rotar credenciales o reconectar el número con otra cuenta conserva la conexión, y un Content de otra cuenta no sirve. Desconectar da de baja la caché.
  - **No hay purga**: son como mucho 13 formas por conexión.
  - En sesión: de 1 a 3 botones y de 1 a 10 filas. Un id de más de 200 caracteres o un título vacío es un error; el id no se trunca porque tiene que volver igual en `ButtonPayload` o `ListId`.
  - Un Content borrado en la consola se recrea una vez, ante un 404 o un 20404.
- **§9, opciones directas fuera de la ventana.** Con la ventana cerrada no sale nada y queda `sin_plantilla`: esa plantilla es la contraparte de Meta (ADR-0172), que una conversación de Twilio no usa aunque conserve un `meta_connection_id`. Los avisos por evento con opciones siguen sin puente y usan su plantilla de Content.
- **§6, sin tabla `twilio_webhook_events`.** La auditoría reutiliza la tabla de webhooks con `meta_app_id = "twilio:<AccountSid>"` y solo campos de diagnóstico. Son tres tablas nuevas, no cuatro.
- **§8, sin `core/gateway/templates/resolver.py`.** `core/gateway/twilio/plantillas_en_uso.py` tiene lo de Twilio, y cada consumidor (selector del chat, mapa y avisos por evento, segmentos) ramifica por proveedor. Así el camino de Meta no se toca. El selector del chat mantiene su contrato: no se agregan `provider` ni `template_ref`.

### Precisiones

- **§3.** Credenciales que Twilio rechaza responden 422 `twilio_credentials_invalid`, no 401: un 401 de nuestra API significa sesión vencida.
- **§4.** El índice único global es `uq_channel_connections_twilio_sender_viva`, sobre `channel_connections`, no sobre `twilio_connections`. Regla nueva: **una cuenta de Twilio, un negocio** (`twilio_account_en_otro_negocio`, SECURITY DEFINER).
- **§5.** Las URL registradas llevan *connection overrides* en el fragmento (`#rc=3&rp=5xx,ct`), para que Twilio reintente ante 5xx o timeout de conexión. La firma se valida contra la URL guardada con y sin ese fragmento.
- **§8.** `twilio_content_templates` lleva la columna `origen` (`solvershub` o `twilio`), que distingue las creadas aquí de las adoptadas por el sync. El sync solo da de baja con la lista completa. Solo se usan plantillas de texto (`twilio/text`): una con botones o media enviaría algo distinto de lo que se muestra.
- **§8, al enviar.** `plantillas_en_uso.enviable` revalida que el `ContentSid` siga vivo, aprobado y de texto en la conexión **actual** de la conversación; si no, el error es permanente.
- **§10.** El 63049 es `PERMANENT` y no da de baja: lo decide Meta por envío. Un `ChannelError` de plantilla o de interactivo es permanente, también en Meta (antes era transitorio y terminaba en la DLQ).
- **Segmentos (PR-7b, ADR-0150).** Salen por Twilio con sus plantillas `MARKETING` aprobadas; `segment_sends.template_id` apunta a la tabla según el proveedor de la conexión. Un 21610 en un mensaje de segmento da de baja al cliente con origen `twilio_21610`, sin escalar. Un destinatario cuya conversación cambió de conexión tras la vista previa se omite (`otra_conexion`, también en Meta).
- **Bandera.** Las rutas nuevas no la exigen: abre el alta. Con la conexión creada, los cortes son la pausa y la suspensión del negocio.

### Pendiente de verificar en test con el número real

1. Que Twilio acepte variables en `actions[].title`, `actions[].id`, `items[].item` e `items[].id`, y saltos de línea en la variable del cuerpo. **Todo el diseño de §9 depende de esto**: si las rechaza, se reabre el Content por estructura.
2. Qué código devuelve Twilio al enviar con un `ContentSid` borrado.
3. Qué campos trae el webhook al elegir una fila de lista (`ListId` no está documentado).
4. Que la Senders API acepte el fragmento `#rc=…` en las URL.
5. Si `price` incluye el cargo de Meta además de la tarifa de Twilio.

La baja por 21610 solo se registra cuando el error llega al crear el mensaje; si llegara por el status callback no se registra, igual que el 131050 de Meta por webhook.

**Reemplaza un criterio de revisión de arriba:** «que más del 5 % de los envíos interactivos caiga a texto» ya no aplica, porque no hay caída a texto. Se revisa si Twilio rechaza una parte relevante de los interactivos.
