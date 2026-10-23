## Messenger disponible en el catálogo de conexiones (ADR-0152, HU-CHAN-01) (2026-10-23)

`messenger_meta` pasa a `available = true`: aparece en «Agregar conexión» y el panel lo lleva a
`/conexiones/messenger` (solvershub-panel). El catálogo se espeja a la base en el arranque
(`sync_connection_types`), sin migración. Instagram sigue `available = false` hasta HU-CHAN-04.

- **`META_MESSENGER_CONFIG_ID` ya no tiene respaldo en `META_CONFIG_ID`.** Esa es la Login Configuration de
  Embedded Signup de WhatsApp: abriría el registro de WhatsApp y no concede los permisos de Páginas. Sin la variable
  el alta responde `meta_configured = false` con `missing_settings = ["META_MESSENGER_CONFIG_ID"]` y la pantalla lo
  explica. **Antes de desplegar a test hay que crearla en Meta y ponerla en el entorno.**
- Quien no tiene Messenger en su plan (Free) ve el tipo en el catálogo; la pantalla le explica que es de los planes de
  pago y la API rechaza el intento con `plan_channel_not_included`.
- Prueba nueva: todo tipo de mensajería de Meta disponible debe tener adaptador de salida.
- El panel clásico (en retiro) muestra Messenger deshabilitado con «Solo en el panel nuevo»: su alta vive allí y la
  tarjeta no puede llevar a un enlace vacío.
- **Los ecos del webhook ya no se procesan como mensajes del cliente.** La Página se suscribe a `message_echoes`
  (lo que la propia Página envía vuelve al webhook con `message.is_echo`) y el parser lo tomaba como un mensaje
  entrante cuyo remitente es la Página: el asistente se habría respondido a sí mismo. Ahora se descarta.
