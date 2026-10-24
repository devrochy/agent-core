## Agent OS: toda conversación nace con conexión (requisito del contract, HU-AOS-15)

Prepara el NOT NULL de `conversations.connection_id` (ver adenda 2026-10-24 de ADR-0115). Sin cambios con `AOS_CONNECTION_ROUTING` apagada, salvo la conexión interna del Centro de pruebas.

- **Centro de pruebas:** cada negocio tiene una conexión interna `playground` (tipo nuevo, oculta, sin credenciales ni release, no conversacional), creada al primer uso. La conversación de prueba va por ella; la de antes sin conexión se liga al usarla. `backfill-conexiones` suma el paso 5 para ligar las de prueba que ya existen (también cerradas).
- **Consumer:** con `AOS_CONNECTION_ROUTING` encendida, un número de la cuenta sin fila canónica la recibe con su primer mensaje y la conversación nace con conexión. Nuevo log `conversacion_sin_conexion` para lo que quede sin ella.
- La conexión interna no aparece en conexiones manuales, consola de conexiones ni filtros de Conversaciones.
- El consumer relee bajo el candado: no religa una fila que otra alta dejó viva, no crea para un número liberado ni de una cuenta dada de baja, crea con instancias y solo para WhatsApp. El turno de prueba no es un `release_missing`; la conexión interna no se guarda a mano ni envía por ningún proveedor.
