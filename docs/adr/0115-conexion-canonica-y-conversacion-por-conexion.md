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
