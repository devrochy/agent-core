## El chat web es una conexión y las altas nuevas nacen con sus agentes (2026-10-11)

- HU-AOS-11 segunda parte (ADR-0115, adenda). El widget de chat web pasa a ser una conexión `webchat`
  (`channel_connections`): clave pública aleatoria generada por el servidor, secreto de Turnstile cifrado en
  `credentials_encrypted`, dominios en `settings`. Guardar Configuración → Chat web escribe `app_settings` como
  antes y, en la misma transacción, la conexión. Nada cambia para el negocio ni para el visitante.
- Migración **0086**: tipo `webchat` en el catálogo y resolvedor público `webchat_resolve` (`SECURITY DEFINER`,
  solo datos no secretos). El endpoint público resuelve el widget por su conexión y cae a `app_settings` solo si
  el negocio nunca tuvo una. El polling del widget filtra por conexión: con dos widgets un visitante no ve la
  conversación del otro.
- `agent-core db backfill-conexiones` convierte el widget de `app_settings` en conexión y liga las conversaciones
  web sin conexión (idempotente, con `--dry-run`). `backfill-agentes` ya no avisa «el chat web… queda sin
  Coordinador»: bloquea ese negocio hasta correr `backfill-conexiones`, y luego el widget recibe Coordinador y
  General como las demás conexiones.
- Con `AOS_CONNECTION_ROUTING` encendida, la conversación web sin conexión del visitante se adopta en la de su
  widget (no abre otra ni gasta cuota). Apagada, no cambia nada.
- Altas posteriores: los números nuevos que trae la sincronización con Meta (`sync_phone_numbers`) nacen con su
  conexión canónica; el widget, las conexiones manuales y `waweb` que se crean o reviven después del backfill
  reciben Coordinador y General si el negocio ya tiene agentes. La release de esas conexiones sigue saliendo de
  `agent-core db backfill-releases` (HU-AOS-15).
- Orden de despliegue: migración 0086 antes del código; después, `backfill-conexiones --dry-run`, revisar y
  correr, y luego `backfill-agentes` y `backfill-releases` en los negocios que ya los tenían.
