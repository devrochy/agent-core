# ADR-0115 — Conexión canónica y conversación por (conexión, cliente)

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-11 (SPEC-26). Enmienda ADR-0069 (catálogo de tipos de conexión), ADR-0045 (medición de
  conversaciones), ADR-0122 §6 (altas y reconexión de números) y ADR-0127 §3 (número del envío y promoción).
  Usa ADR-0126 (conexión dueña del número) y ADR-0122 (pausa, sin cambiarla). Respeta ADR-0095 y es consistente
  con ADR-0128 §3 (`different_number`).

## Contexto

- Conviven dos modelos de conexión, `channel_connections` (genérico) y `meta_connections` (cuenta de Meta), con un puente doble en `conversations`.
- Desde ADR-0126 el webhook identifica la conexión dueña de cada grupo para verificar la firma (`webhook_secrets.conexiones_del_grupo`, "el número manda"), pero no la propaga: el consumer la resuelve por `connection_external_id`.
- La conversación abierta es una por cliente y se re-asigna a la última conexión por la que escribió (`core/orchestrator/consumer.py::_resolve_conversation`).
- Las guardas de pausa de ADR-0122 deducen "es una ruta de Meta" de `conversations.connection_id IS NULL` (`meta_connection_state.motivo_pausa_de_conversacion`, `sender._registrar_si_pausada`, `tasks/outbound_send.py`).
- El sender envía por el primer número vivo de la cuenta (ADR-0127 §3: la conversación no guarda el número).
- El chat web es configuración en `AppSettings`, resuelta por `web_chat_resolve` (migración 0048), no una conexión.

## Decisión

La conexión es `channel_connections`, y hay una conversación abierta por (conexión, cliente).

1. **`channel_connections` es la Conexión:** un extremo conversacional (número, página, widget o buzón). `meta_connections` queda como cuenta del proveedor, enlazada por `meta_connection_id` con FK compuesta `(tenant_id, meta_connection_id)`.
   - Hay **una conexión por número** (fila viva de `phone_numbers`), estable aunque el número cambie de cuenta: revivir un número o pasarlo a otra cuenta actualiza `channel_connections.meta_connection_id`. Cuando el sender y las guardas de pausa resuelven la cuenta por `channel_connections.meta_connection_id` (punto 2), `conversations.meta_connection_id` deja de leerse y `meta_asset_claims.mover_conversaciones` (que hoy reescribe ese campo) se elimina en ese mismo cambio, no antes: si no, tras una reconexión la conversación apuntaría a una cuenta sin números vivos y el envío fallaría.
   - Las filas con `meta_connection_id` **no guardan credenciales** (CHECK `meta_connection_id IS NULL OR credentials_encrypted IS NULL`), nacen con `is_primary = false` y la API de conexiones manuales no las edita: las credenciales siguen la regla de origen de ADR-0094.
2. **Ciclo de vida.** La pausa sigue siendo la de ADR-0122: una conexión con `meta_connection_id` está pausada si `motivo_pausa` de esa cuenta lo dice. `channel_connections` no agrega estado propio para Meta (`active`/`deleted_at` solo para conexiones sin cuenta: manual, `waweb`, `webchat`). **Antes del backfill**, las guardas que usan `connection_id IS NULL` y leen `conversations.meta_connection_id`, y `sender.resolve_credentials`, pasan a resolver la cuenta por `channel_connections.meta_connection_id`. Las guardas son `motivo_pausa_de_conversacion`, `_registrar_si_pausada`, `outbound_send`, `event_notices` y `conversations._rechazar_si_conexion_pausada`. Si no, la pausa se saltaría en silencio y los envíos escalarían.
3. **Integraciones** (calendario, correo saliente): siguen siendo del negocio y no tienen agentes; el gating de ADR-0072 no cambia. Ligar una integración a una conexión se decide cuando un negocio con varios números lo pida.
4. **Chat web** como tipo de conexión `webchat`. El endpoint público resuelve la conexión por su clave con un resolvedor `SECURITY DEFINER` sobre `channel_connections` (reemplaza `web_chat_resolve`), con índice único parcial **global** de la clave para `type_id = 'webchat'` (el endpoint público resuelve sin negocio; hoy `web_chat_resolve` hace `limit 1` sin unicidad); el aislamiento lo cubre aparte el índice que empieza por `tenant_id`. El secreto de Turnstile pasa a `credentials_encrypted` y el polling de mensajes filtra por conexión (con dos widgets, un visitante no ve la conversación del otro).
5. **Ruteo:**
   - El consumer resuelve la conexión por `connection_external_id` (`phone_number_id` es único por negocio desde la migración 0063), y hay **una conversación abierta por (conexión, cliente)**. El webhook no cambia.
   - El sender envía por el número de la conexión de la conversación (`channel_connections.external_id`), no por el primer número vivo de la cuenta.
   - La memoria del cliente sigue siendo del negocio.
   - La métrica `conversaciones` (ADR-0045) cuenta una por conexión: un cliente que escribe a dos números inicia dos.
6. **Migración:**
   - *expand* y *backfill* por lotes e idempotente: cada número vivo de WhatsApp (BYOK, Embedded Signup o manual) recibe su conexión y el chat web la suya. El *dry-run* reporta los choques con una conexión manual del mismo `external_id` (`uq_connection_tenant_provider_external`) y no crea una segunda fila. Messenger e Instagram (`fb_pages`, `ig_accounts`) quedan fuera hasta que haya un cliente;
   - **altas posteriores:** toda alta o reconexión de un número crea o revive su fila de `channel_connections` en la misma transacción de `claim_assets` (`meta_asset_claims.numero_para_conexion`, los cinco caminos de ADR-0122 §6). La promoción de ADR-0127 §3 crea la conexión del número real con sus instancias y su release clonadas de la de prueba. El backfill solo cubre lo que existía antes del despliegue;
   - *switch* detrás de `AOS_CONNECTION_ROUTING`: el consumer resuelve por (conexión, cliente) y deja de reescribir `connection_id`. Hasta el *contract*, la búsqueda tolera duplicados y toma la conversación abierta más reciente (apagar y volver a encender la bandera puede dejar dos). Apagar la bandera es seguro hasta el *contract*: todavía no hay índice único, y el camino anterior toma la conversación abierta más reciente (`consumer.py::_resolve_conversation`);
   - la vuelta atrás va en orden inverso: primero `AOS_RUNTIME` (ADR-0112) y después esta bandera. El camino anterior se modifica para que, al reescribir `connection_id`, limpie `release_id`, porque esa release es de la conexión anterior y la FK compuesta lo rechazaría;
   - *contract* tras 2 releases estables: se deduplican sin cerrar ninguna. En cada (conexión, cliente) con más de una abierta (`status IN ('open','escalated')`) queda la más reciente; las demás pasan sus mensajes a esa y se marcan con `deleted_at`; sus tareas y `task_events` no se mueven (append-only). Después se crea el índice único parcial con el mismo predicado y `connection_id` pasa a obligatorio. Desde ahí no se vuelve al camino anterior.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla nueva `connections` | Toca todo el ruteo y el *sender* |
| Mantener el puente doble | No hay "la" conexión de un mensaje ni un Coordinador por conexión |
| Conversación por cliente | Rompe "un Coordinador por conexión" y ya causó respuestas por el canal equivocado |
| Crear el índice único en el *switch* | Con la bandera apagada, el camino anterior reescribe `connection_id`, choca con el índice y el mensaje termina en la DLQ |

## Consecuencias

**Positivas:** cada conexión tiene su Coordinador, sus agentes y su release; el cliente recibe respuesta por donde escribió; el chat web puede tener varias instancias.

**Negativas / lo que aceptamos pagar:** un cliente que escribe a dos números tiene dos conversaciones y consume dos de la cuota (la memoria del cliente las une); la migración de datos va detrás de una bandera; después del *contract* no hay vuelta atrás por bandera.

**Qué haría que revisáramos esto:** que el ruteo por conexión falle con activos de Meta compartidos (ADR-0095), que el backfill deje conversaciones sin conexión, o que contar por conexión encarezca la cuota de forma injusta para negocios con varios números.

## Impacto en el código

- `core/orchestrator/consumer.py` (`_resolve_conversation`);
- guardas de pausa: `core/gateway/meta_connection_state.py` (`motivo_pausa_de_conversacion`), `core/gateway/sender.py` (`_registrar_si_pausada`), `core/tasks/outbound_send.py`, `core/gateway/event_notices.py` y `core/api/routers/conversations.py` (`_rechazar_si_conexion_pausada`);
- `core/gateway/sender.py`: `resolve_credentials` y `_resolve_meta_connection_credentials` (cuenta y número desde la conexión de la conversación);
- `core/gateway/meta_asset_claims.py` (`numero_para_conexion` crea o revive la conexión; `mover_conversaciones` se elimina junto con la última lectura de `conversations.meta_connection_id`) y la promoción de ADR-0127;
- `core/api/routers/web_chat.py` y el resolvedor `SECURITY DEFINER` que reemplaza `web_chat_resolve`;
- `core/gateway/connection_types.py` (`webchat`);
- migraciones y backfill.

## Adenda (2026-10-10, HU-AOS-11 primera parte)

- **Lectores de la cuenta.** Además de las guardas y `resolve_credentials`, la selección de plantillas
  (`event_notices.approved_template_for` y `approved_templates_for_conversation`) usa la cuenta de la
  conexión canónica: la cuenta es una sola para la pausa, el envío y la plantilla.
- **Enmienda a ADR-0095 §2.** En `channel_connections` solo cuentan como dueñas de un número las filas
  **sin cuenta** (`meta_connection_id IS NULL`): migración 0073 en `resolve_tenant_for_phone` y
  `meta_asset_claimed_by_other_tenant`, y `webhook_secrets._manual_del_numero`. La fila canónica sobrevive
  a la baja de su cuenta (no tiene estado propio) y no debe seguir reclamando ni ruteando el número.
- **Con la bandera apagada no es idéntico** (excepción acotada a la frase "sin cambio de conducta" de
  ADR-0112): desde el despliegue, un número dado de alta o reconectado tiene su conexión canónica, el
  camino anterior la pone en `connection_id` y la respuesta sale por el número de la conexión (§5), no
  por el primero vivo de la cuenta. Solo difiere en cuentas con varios números.
- **"Revivir" es religar:** la fila canónica no se borra nunca, así que reconectar el número la reasigna
  a la cuenta nueva.
- **Huérfanas con la bandera encendida:** si no hay abierta para (conexión, cliente) y la conexión está
  ligada a Meta, el consumer adopta la abierta del cliente sin `connection_id` cuya cuenta
  (`conversations.meta_connection_id`) es la de la conexión. El *dry-run* del backfill informa las
  abiertas que quedan sin conexión.
- **Despliegue:** workers y consumer antes que la web; en la ventana inversa, un consumer viejo con una
  fila canónica nueva no pausa la entrada, aunque el envío igual falla cerrado.
- **Pendiente para HU-AOS-13/14:** la promoción (ADR-0127 §3) ya crea la conexión del número real por el
  alta BYOK; clonar sus instancias y su release queda como criterio de esas HU.

## Adenda (2026-10-11, HU-AOS-11 segunda parte: chat web como conexión)

- **Chat web = conexión `webchat`** (§4). Tipo nuevo del catálogo (`comunicacion`, canal y
  proveedor `web`, conversacional), fuera del selector de alta del panel (`en_selector = False`): el
  widget se sigue configurando en Configuración → Chat web; la consola de conexiones es HU-AOS-23.
  `external_id` es la clave pública (192 bits, generada por el servidor), `active` es "habilitado",
  los dominios van en `channel_connections.settings` y el secreto de Turnstile, cifrado, en
  `credentials_encrypted`. No es una conexión manual (`connections.ES_MANUAL` la excluye y
  `administrada_aparte` da 404 en la API y el panel de conexiones): no se lista, no cuenta para la
  primacía ni para el gating de ADR-0072, y `save_connection` la rechaza.
  *Nota 2026-10-19:* la consola sí la pausa y la reanuda por `POST /admin/connections/{id}/action`
  (solo `activate` / `deactivate`, con el mismo interruptor que Configuración); ver ADR-0122, adenda 2026-10-19 (b).
- **Resolvedor `webchat_resolve`** (migración 0086, `SECURITY DEFINER`, `search_path` fijo, `EXECUTE`
  solo para `app_runtime`): igualdad sobre el predicado de `uq_cc_webchat_key`, sin `LIMIT`, solo
  conexiones vivas. Devuelve solo lo no secreto (negocio, conexión, habilitada, dominios y si hay
  secreto); el secreto lo lee el POST del visitante con RLS dentro del negocio y falla cerrado (403) si
  no puede leerlo o descifrarlo; el guardado y el backfill lo vuelven a cifrar desde `app_settings`.
- **Expand con doble escritura.** Hasta el *contract*, `app_settings.web_chat_config` sigue siendo la
  fuente de verdad y se escribe como antes (secreto incluido, para que una vuelta atrás del código
  siga sirviendo el widget); cada guardado sincroniza la conexión en la misma transacción. El GET del
  panel sigue leyendo `app_settings`. El endpoint público cae a `web_chat_resolve` (0048) solo si el
  negocio nunca tuvo una conexión `webchat`: un widget borrado no revive por la configuración vieja.
  Tras volver atrás el código y desplegar de nuevo, `backfill-conexiones` resincroniza la conexión
  desde `app_settings` (una rotación hecha mientras tanto solo estaba ahí).
- **Rotar la clave** actualiza `external_id` de la misma conexión: la clave vieja sale del índice
  global. Con 192 bits aleatorios no se reutiliza en la práctica, pero no queda "reservada" como dice
  la HU al pie de la letra; las conversaciones y las instancias siguen siendo de la conexión.
- **Polling por conexión:** el GET de mensajes filtra por la conexión del widget (y las conversaciones
  sin conexión de antes del backfill, del mismo visitante y negocio): con dos widgets un visitante no
  ve la conversación del otro.
- **Huérfanas web con la bandera encendida:** como con Meta, la abierta sin conexión del visitante
  (canal `web`) se adopta en la conexión del widget en vez de abrir otra.
- **Backfill:** `backfill-conexiones` convierte el widget de `app_settings` en conexión y liga las
  conversaciones `web` sin conexión cuando el negocio tiene exactamente un widget; las abiertas que
  quedan sin ligar suman a `abiertas_sin_ligar`. `backfill-agentes` bloquea un negocio con el widget
  guardado sin su conexión (antes era solo un aviso).
- **Altas posteriores** (pendientes de HU-AOS-13 parte 2): `sync_phone_numbers` da de alta los números
  nuevos con `numero_para_conexion` (conexión canónica e instancias en la misma transacción, con el
  candado por número). Las conexiones sin cuenta de Meta que nacen o reviven después del backfill
  (widget, manual, `waweb`) reciben Coordinador y General con `instancias_de_conexion_nueva`, la misma
  frontera de SAVEPOINT que las de Meta, si el negocio ya tiene sus agentes. Decisión revisable.
- **Fuera de esta parte (revisable):** la release al alta de una conexión nueva sigue siendo del CLI
  `backfill-releases` (adenda de ADR-0117: que esos caminos construyan la release solos es de
  HU-AOS-15). El *contract* (deduplicar sin cerrar, índice único, `connection_id` NOT NULL, `VALIDATE`,
  quitar `mover_conversaciones` y `web_chat_resolve`, borrar el secreto en claro de `app_settings`)
  no entra: exige la bandera encendida y 2 releases estables, y deduplicar y crear el índice van en el
  mismo cambio. La verificación con la BYOK real de test antes de encender la bandera sigue abierta.

### Adenda 2026-10-24 · Toda conversación nace con conexión (requisito del contract, HU-AOS-15)

El *contract* vuelve `conversations.connection_id` NOT NULL. Dos caminos todavía insertaban `NULL` y se corrigen antes de aplicarlo (decisión del dueño, 2026-10-24):

- **Centro de pruebas: conexión interna `playground` por negocio.** Tipo nuevo del catálogo (`core/gateway/connection_types.py`, `en_selector = false`, `produces_conversations = false`): una fila por negocio en `channel_connections` (`channel = test`, `provider = internal`, `external_id = playground-<tenant_id>`, sin credenciales ni release), creada al primer uso con candado por negocio (`core/gateway/conexion_pruebas.py`). La conversación de prueba (`channel == TEST_CHANNEL`) se crea con ella y la de antes sin conexión se liga al usarla.
  - **No es un canal:** no entra en `backfill-agentes`, `backfill-releases`, `migrar-a-v3` ni en el contract (todos filtran `produces_conversations`); sin release activa, `asignar` devuelve `None` y `fijar_release_del_turno` lo reconoce por el TIPO de la conexión (no por el canal: un journey de evals en `channel = test` sobre una conexión real sí exige su release) y sigue por el camino de siempre, sin `release_missing` ni dejar al turno sin escrituras de memoria.
  - **Oculta:** `ES_MANUAL` y `administrada_aparte` la excluyen (la API y el panel de conexiones manuales responden 404), la consola de conexiones (`total` y filas) y el filtro de conexiones de Conversaciones no la listan; no tiene instancias de agentes, así que no cuenta en las métricas de conexiones con agentes.
  - **Backfill:** `backfill-conexiones` suma el paso 5, que liga las conversaciones de prueba sin conexión (también cerradas) a la conexión interna, que crea solo si hay algo que ligar.
- **Consumer: el número de la cuenta sin fila canónica.** Con `AOS_CONNECTION_ROUTING` encendida, un mensaje a un `phone_number_id` que tiene su fila en `phone_numbers` pero no en `channel_connections` (se conectó por un camino que no la creó) la crea en ese momento (`asegurar_conexion_en_vuelo`: mismo candado `meta_asset:phone:<id>` que `claim_assets` y el backfill) y la conversación nace con conexión. Con la bandera apagada no cambia nada.
- **El «número de la plataforma» (`.env`) no es un caso de conversación nueva:** `resolve_tenant_for_phone` solo resuelve un negocio por `channel_connections` o `phone_numbers`, así que un mensaje a un número sin ninguna de las dos filas no llega a ningún negocio y no abre conversación. Lo que quede sin conexión se avisa con el log `conversacion_sin_conexion` (antes del contract; después el NOT NULL lo rechaza) y bloquea la precondición 3 del contract.
- **Revisión del auditor (2026-10-24):** `asegurar_conexion_en_vuelo` **relee bajo el candado**: si otra alta o reconexión dejó la fila viva, la devuelve sin religarla; si el número se liberó o su cuenta está dada de baja o en borrador, no crea nada; crea en un SAVEPOINT, con instancias (`instancias_al_alta`) como las altas, y solo para WhatsApp. Además: el sender devuelve `("internal", {})` para la conexión interna (sin caer a la primaria ni al `.env`), `save_connection` la rechaza, y el conteo `conexiones_sin_agentes` del backfill de agentes no la cuenta. Sin cambio, por ahora: `GET /admin/playground` crea la conexión en la primera lectura (ya creaba el cliente y la conversación) y `data_por_agente.consumo()` no filtra el tipo (latente: la conexión interna no tiene release).
- **Prueba:** `tests/integration/test_conversacion_siempre_con_conexion.py` y el ajuste de `test_conexion_canonica.py` (la conversación huérfana de antes del backfill ahora se crea con la bandera apagada, porque encendida ya nace con conexión).
