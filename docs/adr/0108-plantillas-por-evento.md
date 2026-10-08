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
