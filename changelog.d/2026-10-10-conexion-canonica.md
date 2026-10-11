## Agent OS: conexión canónica por número y ruteo por conexión detrás de bandera (2026-10-10)

Primera parte de HU-AOS-11 (ADR-0115): *expand* y *switch* detrás de `AOS_CONNECTION_ROUTING`,
sin *contract*.

- **Pausa y credenciales por la conexión.** Las guardas de pausa de ADR-0122 (consumer y debounce,
  `sender._registrar_si_pausada`, `outbound_send`, `event_notices` y el 409 de la API de
  conversaciones) resuelven la cuenta de Meta con `cuenta_meta_de_conversacion`: si la conversación
  tiene una conexión ligada a Meta, la de `channel_connections.meta_connection_id`; si no, el puente
  `conversations.meta_connection_id`, como antes. `resolve_credentials` de una conexión canónica
  envía con la cuenta de la conexión y **por el número de la conexión**, sin caer a otro número, a
  la primaria ni al `.env`. Las plantillas se buscan en la WABA de esa misma cuenta.
- **Altas posteriores.** `meta_asset_claims.numero_para_conexion` (los cinco caminos de ADR-0122 §6
  y, por el alta BYOK, la promoción de ADR-0127 §3) crea o religa, en la misma transacción, la fila
  de `channel_connections` del número: sin credenciales, `is_primary = false`, tipo `whatsapp_meta`.
  El rollback de un alta BYOK fallida la devuelve a la cuenta anterior. Una conexión manual del mismo
  número gana y no se duplica.
- **Las filas canónicas no cambian lo de antes.** Migración `0073`: `resolve_tenant_for_phone` y
  `meta_asset_claimed_by_other_tenant` ignoran las filas con cuenta (el dueño del número sigue siendo
  `phone_numbers`). La firma del webhook, el listado y la edición de conexiones manuales (API y SSR),
  el gating de tools (ADR-0072), la primaria y los primeros pasos solo miran conexiones sin cuenta.
- **Backfill** `agent-core db backfill-conexiones [--dry-run] [--tenant] [--lote]`: idempotente, por
  lotes y por negocio. Pone `type_id`/`category` a las conexiones viejas sin tipo, crea la conexión de
  cada número vivo de WhatsApp, liga las conversaciones de cuentas con un solo número y reporta los
  choques con conexiones manuales y las conversaciones abiertas que quedan sin conexión. **No se ha
  corrido en ningún entorno.**
- **Ruteo por (conexión, cliente)** detrás de `AOS_CONNECTION_ROUTING` (global, apagada por defecto):
  encendida, una conversación abierta por conexión, sin reescribir `connection_id`, tolerando
  duplicados (la más reciente) y adoptando la abierta del cliente que quedó sin conexión en la misma
  cuenta. Apagada, el camino de siempre, que ahora limpia `release_id`, `release_lane`,
  `active_agent_instance_id` y `owner_since` al cambiar de conexión.
- **Cambio visible con la bandera apagada:** desde este despliegue, un número dado de alta o
  reconectado tiene su conexión canónica, sus conversaciones quedan con `connection_id` y la respuesta
  sale por el número al que escribió el cliente (antes, por el primer número vivo de la cuenta). Solo
  difiere en cuentas con varios números.
- Pendiente (otra parte de la HU): *contract* (dedupe, índice único, `connection_id` NOT NULL),
  eliminar `mover_conversaciones`, chat web como conexión `webchat`, clonar instancias y release en la
  promoción (HU-AOS-13/14) y la verificación con Meta real en test.
