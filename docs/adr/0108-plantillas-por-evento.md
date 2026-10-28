# ADR-0108 — Plantillas de WhatsApp por evento

- **Estado:** aceptado
- **Fecha:** 2026-10-08
- **Contexto:** HU-ARCH-13 (SPEC-02, decisión D-SPEC-02-2: config por negocio, evento → plantilla + variables).
- **Se apoya en:** ADR-0031 (ciclo de vida de plantillas por WABA), ADR-0094 (credenciales por conexión),
  ADR-0107 (outbox de eventos de dominio).

## Contexto

Fuera de la ventana de 24 h, WhatsApp solo acepta plantillas aprobadas (error 131047). Los avisos que
cobran o recuerdan (cargo de suscripción, recordatorio de cita, seguimiento) casi siempre caen fuera de
la ventana y hoy salen como texto libre: quedan `failed`. `sender.send_to_conversation(..., is_template=True)`
era una bandera muerta y el 409 `window_expired` del operador nunca salía, porque el sender se tragaba el
error y el router respondía 204. Las plantillas ya se sincronizan por WABA (ADR-0031), pero nada las envía.

## Decisión

Cada negocio mapea **evento → plantilla aprobada** en `app_settings.message_templates_map` y el core
decide en el momento del envío:

1. **Ventana abierta** → texto de respaldo (`fallback_text`), que es más barato que una plantilla.
2. **Ventana cerrada y evento mapeado a una plantilla `APPROVED` de la WABA de la conexión de la
   conversación** → se envía la plantilla (`OutboundAdapter.send_template`, `type: template` en Graph).
3. **Ventana cerrada sin mapeo, o la plantilla no está aprobada en esa conexión** → mensaje `failed` con
   `error_reason = "sin_plantilla"`. El panel lo muestra (`GET /admin/settings/message-templates/missing`).

Detalle:

- **Forma del mapa:** `{"<evento>": {"template", "language", "variables", "fallback_text"}}`. `variables` es
  una lista (placeholders posicionales `{{1}}…`, en orden) o un objeto `{placeholder: ruta}` (plantillas
  `NAMED`). v1 solo resuelve variables del **cuerpo**; una plantilla con variables en la cabecera o en botones
  no se puede mapear.
- **Variables seguras (invariante 7):** cada evento declara sus **rutas permitidas**
  (`core/gateway/event_notices.py::EVENTOS`, p. ej. `cliente.nombre`, `cargo.monto`). El contexto lo arma el
  servidor a partir del evento y de la base; el texto del LLM nunca entra. Una ruta fuera del catálogo se
  rechaza al guardar el mapa y al enviar.
- **Eventos de v1:** `subscription.charge_created`, emitido por `pack_subscriptions.generar_cargos` por el
  outbox, en la misma transacción que el cargo (`dedupe_key = sub_charge:<cargo>`). Lo consume un handler del
  core (`core/events/core_handlers.py`), registrado en el proceso del despachador. Recordatorios de cita y
  seguimientos de comercio se agregan al catálogo cuando sus HU los emitan.
- **Idempotencia:** el aviso usa un `client_message_id` estable (`<notice_key>` o `<notice_key>:<burbuja>`,
  índice único por tenant). Un reintento del evento no lo duplica.
- **Persistencia:** `messages.template_name/template_language/template_components` (migración `0057`).
  `content` guarda el cuerpo renderizado para el panel. La tarea de envío (`outbound_send._send_one`) toma la
  rama de plantilla cuando hay `template_name`; reintentos y DLQ no cambian.
- **Operador humano:** `send_to_conversation(..., raise_on_window_expired=True)` vuelve a propagar el error, y
  `POST /admin/conversations/{id}/messages` responde 409 `window_expired`. El panel ofrece
  `GET .../templates` (plantillas aprobadas de la conexión) y envía con `POST .../template-messages`; esas
  variables las escribe la persona del equipo.
- **Reglas de seguridad (revisión del 2026-10-08):**
  - una plantilla **nunca** sale por el número de la plataforma (`.env`): `resolve_credentials(...,
    allow_env_fallback=False)`; sin credenciales propias, el envío falla;
  - todo parámetro se limpia (`clean_param`: sin saltos ni tabulaciones, espacios colapsados, máx. 1024).
    `cliente.nombre` se acota a 60 porque `customer.name` lo puede escribir el LLM con
    `update_customer_profile`;
  - una ventana desconocida (`service_window_expires_at` nulo) cuenta como **cerrada**;
  - un aviso que no se pudo encolar (pausa por uso, carrera con la ventana) lanza `NoticeNotSentError`
    para que el outbox lo reintente; no se marca entregado en silencio.
- **Canales:** `send_template` no es abstracto. Solo WhatsApp Cloud lo implementa; los demás canales lanzan
  `ChannelError`, porque no tienen ventana de 24 h.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Plantillas fijas de plataforma | Meta las aprueba por WABA: una plantilla de SolversHub no sirve en la WABA de un cliente (D-SPEC-02-2). |
| Que el LLM redacte el aviso y se envíe "como plantilla" | Rompe la invariante 7 y Meta rechaza cualquier texto que no coincida con la plantilla aprobada. |
| Estado nuevo `sin_plantilla` en `messages.status` | Obliga a cambiar el CHECK y todos los consumidores del estado; `failed` + `error_reason` ya expresa "no salió y por qué". |
| Guardar el mapa en la config de cada pack | Un mismo negocio mapea eventos de varios packs; el envío es del core. Va en `app_settings`, como `web_chat_config`. |

## Consecuencias

**Positivas:** los avisos de cobro llegan fuera de la ventana sin intervención; el operador recibe el 409 y
puede responder con una plantilla; las variables nunca vienen del modelo.

**Negativas / lo que aceptamos pagar:** cada negocio tiene que crear y aprobar sus plantillas en Meta y
mapearlas; sin eso, los avisos fuera de ventana quedan `sin_plantilla` (visibles, pero no enviados). v1 no
resuelve variables de cabecera ni de botones (un link dinámico va en el cuerpo).

**Qué haría que revisáramos esto:** que más del 20 % de los avisos de un mes queden `sin_plantilla` en
negocios con plantillas mapeadas (el modelo de mapeo es difícil de usar), o que Meta exija variables de
botón para las plantillas de cobro (habría que ampliar `build_components`).

## Impacto en el código

- `alembic/versions/0057_plantillas_por_evento.py`, `core/db/models.py` (`AppSettings`, `Message`).
- `core/gateway/channels/base.py`, `core/gateway/channels/whatsapp/outbound.py` (`send_template`).
- `core/gateway/sender.py` (`raise_on_window_expired`, `client_message_id_prefix`,
  `send_template_to_conversation`), `core/tasks/outbound_send.py` (rama de plantilla).
- `core/gateway/event_notices.py` (catálogo, variables, `send_event_notice`), `core/events/core_handlers.py`,
  `core/tasks/domain_events_dispatch.py` (registro de handlers del core).
- `core/shared/events.py` (`ON_SUBSCRIPTION_CHARGE_CREATED`), `pack_subscriptions/jobs.py` (emisión).
- `core/api/routers/conversations.py` (409, plantillas de la conversación, envío del operador),
  `core/api/routers/settings.py` (`/settings/message-templates` y `/missing`).

## Adenda (2026-10-28) — avisos por canal de texto (ADR-0178, pista D2)

Hasta aquí un aviso solo buscaba conversaciones de `whatsapp`: en chat web, Messenger o Instagram se perdía en
silencio, y en QR (waweb) la ventana desconocida lo dejaba como `sin_plantilla`. Con la bandera por negocio
`tenants.settings.avisos_por_canal` (APAGADA por defecto; solo el master, `PUT /admin/platform/tenants/{id}/avisos-por-canal`)
el aviso también sale como texto por esos canales. **Con la bandera apagada nada cambia** (el cuerpo de
`send_event_notice` es el de siempre; `por_canal=False` es el valor por defecto).

- **Texto por canal.** Clave opcional `fallback_text_by_channel` en `message_templates_map[<evento>]`, por tipo de conexión
  (`whatsapp_waweb`, `webchat`, `messenger_meta`, `instagram_meta`), con las mismas `{ruta}` permitidas del evento y el mismo
  render que `fallback_text` (`core/kernel/mensajes`). El `fallback_text` general NO se usa en otros canales: el negocio
  elige qué le escribe a sus clientes por QR o web.
- **Cascada** (`core/gateway/avisos_canal.py`, con la tabla de capacidades de C1): WhatsApp Meta → como siempre (plantilla
  aprobada fuera de la ventana, texto dentro). Canal sin plantillas y con texto definido → ese texto. Sin texto definido →
  no se envía, y queda `event_notice_omitido_por_canal` en el log y la métrica `event_notice_omitted_total{evento,canal,motivo}`
  (antes, silencio). Para chat web, Messenger e Instagram no se escribe un `failed`/`sin_plantilla`: esa alerta del panel remedia
  con plantillas de Meta, que esos canales no tienen. **Excepción conocida:** por QR sin texto definido, o con un evento de
  cobranza, se conserva el camino de antes, que sí deja un `failed`/`sin_plantilla` visible (el panel dirá «falta plantilla» en un
  canal sin plantillas; queda para cuando se decida el destino de ese camino).
- **Políticas de cada canal, intactas.** *QR (ADR-0074/0103):* un aviso es un envío no solicitado y el modo por defecto es «solo
  respuesta», así que solo sale con `waweb_mode = allow_outbound`, con cupo del día y sin pausa (se mira antes de encolar: el
  conector difiere un mensaje bloqueado hasta ~24 h, 00:02 UTC); el delay humano y el contador los sigue poniendo el conector.
  *Messenger e Instagram:* solo con la ventana de 24 h abierta (el asistente nunca escribe fuera, ADR-0152); ventana desconocida =
  cerrada, como con WhatsApp. *Chat web:* sin ventana ni plantillas.
- **Cobranza.** Quien decide CUÁNDO se emite un aviso de cobro sigue siendo `pack_subscriptions` (HU-SUB-18: cron, diferido
  del cargo nuevo y reenvío del panel miran el horario legal); este camino no lo cambia. Los eventos `subscription.charge_created`,
  `charge_reminder` y `paused` **no salen por QR** (la API rechaza guardarles texto de QR): el diferimiento del conector podría
  entregarlos fuera del horario y no se puede garantizar. Costo: un negocio solo-QR no recibe recordatorios de pago por ahí. Lo
  destraba una enmienda a ADR-0103 para que `outbound_send` falle rápido, en vez de diferir, los mensajes de cobranza. Sí salen por
  web, Messenger e Instagram, que entregan al instante.
- **Riesgos aceptados (QR).** `limits.allow_send` lee el contador sin reservar: una ráfaga puede pasar el chequeo y el conector
  diferir los últimos (hasta ~24 h). Y los avisos comparten el tope de 50/día con las respuestas del bot: una tanda grande puede
  dejarlas sin cupo. Por eso `waweb_mode = allow_outbound` es una decisión explícita del negocio.
- **Criterio de retiro de la bandera.** Es un guard de despliegue: cuando los avisos por canal se prueben en test y estén en uso,
  se retira (el opt-in queda en el texto por canal y, en QR, en `allow_outbound`). Mientras tanto un texto guardado sin bandera se
  guarda pero no se envía; el master la enciende por negocio.
- **Ventana.** Para estos avisos una ventana desconocida cuenta como cerrada (como en ADR-0108), mientras que
  `sender.validate_service_window` la trata como abierta: para avisos lo estricto es lo correcto. Messenger e Instagram figuran con
  `ventana_24h=False` en la tabla de C1 (allí significa «ventana + plantilla»), por eso `avisos_canal.CON_VENTANA` los declara.
- **Fuera de alcance de D2.** El reenvío manual del panel (`pack_subscriptions`, `admin_api`) sigue buscando solo WhatsApp:
  responde `sin_conversacion_whatsapp` aunque el cliente tenga chat web. No hay evidencia de un negocio que pida avisos por
  Messenger, Instagram o web; QR es el caso real. Los textos por canal son hasta 4 por evento.
- **Qué conversación.** Se busca la de WhatsApp como siempre; solo con la bandera, si el cliente no tiene, la más reciente de
  web/Messenger/Instagram (prefiriendo una con texto definido para ese evento).
- **Pendiente (D3).** Que el mapa de eventos apunte a una plantilla con opciones; estos textos no llevan botones.

## Adenda (2026-10-28) — un evento puede apuntar a una plantilla con opciones (ADR-0178, pista D3)

Une los avisos por evento con las plantillas con opciones de ADR-0172. Clave opcional y compatible hacia atrás
`option_template` en `message_templates_map[<evento>]`: la clave de una plantilla con opciones PUBLICADA del negocio.
Funciona solo con las DOS banderas del negocio encendidas (`avisos_por_canal` de D2 y `opciones_directas` de ADR-0172, las
dos del master y apagadas por defecto); con cualquiera apagada el aviso es EXACTAMENTE el de siempre (prueba de regresión de
los 8 eventos). Código: `core/gateway/avisos_opciones.py`, llamado desde `send_event_notice`.

- **Qué sale.** El aviso es la plantilla con opciones (su menú es el aviso: no se manda además el texto de respaldo), por la
  cascada de `enviar_opciones` (C1/C2): interactivo con la ventana de 24 h abierta, plantilla de Meta aprobada `<clave>_v<n>`
  con la ventana cerrada, menú numerado de texto en QR, chat web, Messenger e Instagram. WhatsApp por Twilio/Plivo, correo y
  el playground no entran.
- **Contexto del servidor.** Las variables y los argumentos de las acciones salen del contexto que arma el handler del evento,
  restringido a las rutas de ese evento que están en `kernel/opciones/contexto.RUTAS_DE_DATOS` (las rutas de `EVENTOS` de los
  eventos que no son de cobranza, menos la base). Nunca del texto del cliente ni del LLM. Ninguna ruta es un id, así que no hay
  un id ajeno que pueda llegar a un argumento de tool. `EVENTOS` y `COBRANZA` viven ahora en `core/kernel/mensajes/eventos.py`
  (y se reexportan).
- **Cuándo cae al camino de siempre** (D2 o legacy, sin dejar un `failed` de más): plantilla sin publicar, retirada, borrada
  o de otro negocio; contexto incompleto (la plantilla pide un dato que el evento no trajo); sin plantilla de Meta aprobada;
  cola que rechaza; QR con la política antibaneo en contra; Messenger/Instagram sin ventana; **conversación con actividad en los
  últimos 15 minutos en un canal de texto** (un menú fuera de turno se vuelve el último saliente y se comería el «1» que el
  cliente dirigía al agente, adenda C2). Queda `event_notice_opciones_fallback` (log) y `event_notice_options_fallback_total`
  (métrica) con el motivo, nunca valores. Un envío ya hecho (reintento del outbox) vuelve como `duplicado`; un resultado
  ambiguo (`internal_error`, que pudo ocurrir tras encolar) NO cae al texto: se reintenta (el `send_id` es determinista).
- **Cobranza excluida.** `charge_created`, `charge_reminder` y `paused` no pueden apuntar a una plantilla con opciones, en
  ningún canal (el PUT lo rechaza y el envío lo ignora si el mapa se guardó a mano). Precaución propia de D3, más estricta que
  D2: aquí el aviso suma caminos de entrega (cola diferida de QR, reintentos del outbox, menús que el cliente contesta días
  después) cuyo horario legal (Ley 2300, HU-SUB-18) no se puede garantizar. Reabrirlo exige una garantía de horario en la
  entrega.
- **`PUT /settings/message-templates`** valida `option_template` (forma, existe en el negocio, publicada con versión vigente
  no retirada, no borrada y que los datos que cita los tenga el evento); omitirlo conserva lo guardado y `""` lo borra.
- **Límites conocidos.** (1) Si `opciones_directas` se apaga entre el primer envío y un reintento, el reintento cae al texto
  y el cliente recibe dos avisos (ventana de segundos). (2) La validación del PUT envejece: si después se retira la plantilla
  o se publica una versión que cita otros datos, el aviso cae al texto (visible en la métrica, aún no en el panel). (3) La
  única tool apta hoy, `list_client_appointments`, no recibe argumentos: de D3 solo se aprovechan hoy las variables del cuerpo y
  las citas `{ctx.x}`; las rutas de evento como `args_contexto` quedan listas para la primera tool que las pida (los valores
  pasan por `clean_param` y por el schema de la tool; revisar si alguno proviene de texto libre del cliente antes de marcar
  una tool apta). (4) `enviar-prueba` y la tool del agente no pasan `datos`: una plantilla que cita datos de un evento no se
  puede probar desde el editor (siempre `context_incomplete`); falta una vista previa con valores de muestra.
- **Sin migración.** OpenAPI: campo `option_template` en `EventTemplateEntry`.
