## Envío a segmentos por WhatsApp vía Twilio (PR-7b, ADR-0186) (2026-11-08)

- **Envío de una plantilla a un segmento** (HU-CRM-06, ADR-0150): ahora también sale por una conexión de Twilio.
  - `GET /admin/leads/acciones/plantilla/conexiones` lista las conexiones de Twilio con sus plantillas `MARKETING` aprobadas y de texto.
  - La vista previa, la confirmación y los lotes son los mismos que en Meta: mismo tope, consentimiento, 7 días entre promociones e idempotencia por destinatario.
  - Cada mensaje lleva el `ContentSid` y las variables; al enviar se revalida en la conexión actual, como en el chat.
  - Sin migración: `segment_sends.template_id` apunta a `message_templates` o a `twilio_content_templates` según el proveedor de la conexión del envío.
- **Pausa**: una conexión de Twilio pausada no arma el envío (409 `connection_paused`), y su motivo aparece en la lista de conexiones.
- **Baja por el proveedor**: un error `OPT_OUT` de Twilio (21610) en un mensaje de segmento da de baja al cliente una sola vez, con origen `twilio_21610`.
  - No escala la conversación, igual que el 131050 de Meta.
  - Esa baja no se deshace desde la ficha (409 `baja_del_cliente`).
  - El 63049 de Twilio no da de baja: lo decide Meta por envío, no el cliente.
- El camino de Meta no cambia: `_baja_por_meta` solo se generaliza a `_baja_por_proveedor`.
- **Panel**: `consentimiento.tsx` todavía no tiene etiqueta para `twilio_21610`; va con el panel P-3.
- **Sin límite diario en la vista previa para Twilio**: `limite_diario` sale de `phone_numbers`, que no tiene el número. Solo es informativo.
