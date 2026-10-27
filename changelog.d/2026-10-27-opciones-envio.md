## Opciones directas: el envío de la plantilla con opciones (SPEC-40, HU-OPC-02, 2026-10-27)

- **Pendiente de verificar con Meta:** el payload de un botón QUICK_REPLY de plantilla se manda **al enviar**
  (`components` con `sub_type: "quick_reply"` y un parámetro `payload`). Es una suposición del diseño que no se
  pudo confirmar contra la documentación oficial; la construcción vive en un solo punto
  (`envio.componentes_con_payload`) y hay una prueba real omitida por defecto
  (`tests/test_opciones_envio_meta_sandbox.py`, solo con `OPCIONES_META_SANDBOX=1`). Hasta verificarla, la bandera
  `opciones_directas.habilitada` se queda apagada: con ella apagada **no sale nada** a ningún cliente.
- `sender.send_options_to_conversation(conversación, clave)` crea el `option_sends` (versión vigente publicada,
  contexto resuelto por el servidor, `expires_at` según `ttl_horas`, un payload `opt:` firmado por opción) y envía
  por la cascada: ventana de 24 h abierta en WhatsApp Cloud → interactivo (`button` hasta 3 opciones, `list` de 4
  a 10); ventana cerrada o desconocida y plantilla de Meta de esa versión `APPROVED` en la WABA de la conversación
  y compatible → plantilla con sus botones y payloads; canal sin botones → texto numerado **sin ejecución
  directa**; ventana cerrada sin plantilla utilizable → no sale nada y queda un mensaje `failed` con
  `sin_plantilla`. Con la bandera apagada se niega (`flag_off`) sin crear ni enviar nada.
- Contexto del envío (`core/kernel/opciones/contexto.py`): `cliente.nombre` y `negocio.nombre` los resuelve el
  servidor; el código que dispara el envío puede aportar más rutas (`datos`). Si falta un dato que el cuerpo o una
  acción necesitan, no se envía. Nunca del LLM ni del texto del cliente.
- Nueva tool del core `enviar_plantilla_opciones(clave)`: solo envía a la conversación del turno una plantilla ya
  publicada (no crea ni edita), se ofrece únicamente con la bandera del negocio encendida y **no** es elegible como
  acción de una opción. Idempotente por llamada de la tool.
- Observabilidad: log `option_send` por envío (negocio, conversación, `send_id`, clave, versión, canal, vía,
  resultado), métrica `option_sends_total{canal,via}` y auditoría `option_send` con ids y claves, sin valores.
- `send_interactive_to_conversation` acepta `client_message_id` estable; el interactivo lleva el pie
  (`footer`) de la definición, que el adaptador descartaba; el texto de respaldo numera las opciones.
