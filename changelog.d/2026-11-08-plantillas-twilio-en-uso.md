## Plantillas de Twilio en el chat y en los avisos por evento (PR-7a, ADR-0186) (2026-11-08)

- **Selector del chat** (`GET /admin/conversations/{id}/templates` y `POST .../template-messages`): en una conversación de Twilio lista las plantillas aprobadas de su conexión y encola la elegida. Las variables son `{{1}}`, `{{2}}`… y se exigen todas.
- **Avisos por evento** (ADR-0108): con la ventana cerrada, una conversación de Twilio usa la plantilla aprobada de su conexión con los valores del servidor (`resolve_values`). Los resultados son los mismos que en Meta: `plantilla`, `sin_plantilla` o `duplicado`.
- **Mapa de avisos** (`/admin/settings/message-templates`): lista y acepta plantillas de Twilio y valida sus variables.
- **Mensaje encolado**:
  - `template_name` lleva el nombre legible, que usan el panel, la línea de tiempo y el crédito.
  - `template_components` lleva un único componente `twilio_content` con el `ContentSid` y las variables.
  - `_send_one` lo envía por `send_content_template`.
- **Errores de envío de plantillas**: una plantilla que el canal no puede enviar (`ChannelError`) ahora es **permanente**, también en Meta. Antes caía en el `except` genérico y se reintentaba como si fuera un error de red.
- **Capacidades**: `whatsapp_twilio` pasa a tener plantilla aprobada y ventana de 24 h. Sigue fuera del puente de avisos con opciones hasta que lleguen botones y listas (PR-7i).
- El camino de Meta (`approved_template_for`, `build_components`) no cambia.
- **Auditoría multitenant** (sin hallazgos altos):
  - Las variables de las plantillas de Twilio se limpian con `clean_param`, como en Meta, y la vista previa usa los mismos valores.
  - Solo se usan y se importan plantillas **de texto**. Una con botones, tarjeta o media enviaría algo distinto de lo que se muestra; quedan para el PR-7i.
  - Una plantilla de Twilio que no se puede enviar se registra como error de Twilio y no de Meta.
  - Nuevo test con dos conexiones de Twilio en el mismo negocio.
