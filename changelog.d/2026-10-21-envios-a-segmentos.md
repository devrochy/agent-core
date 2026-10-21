## Clientes: plantilla a un segmento y consentimiento para promociones (HU-CRM-06, ADR-0150)

- `GET /api/v1/admin/leads/acciones/plantilla/conexiones`, `POST …/vista-previa` (con los filtros de
  Clientes), `POST …/{envio_id}/confirmar` (`Idempotency-Key`) y `GET …/{envio_id}`: solo propietario o
  master. Tope de 200 destinatarios, un envío en curso por conexión y 7 días entre promociones al
  mismo cliente. Siempre plantilla `MARKETING` aprobada.
- Envío por lotes de 25 en el outbox (`crm.segment_send.batch`), idempotente por destinatario
  (`client_message_id = segmento:<envio>:<cliente>`); una promoción que no llega no escala.
- Consentimiento (`GET/POST /api/v1/admin/leads/{id}/consentimiento`) en `customer_consents`, solo
  inserción. Baja por palabras clave del cliente (`behavior_config.palabras_de_baja`) y por el error
  131050 de Meta.
- Línea de tiempo: plantillas enviadas al cliente (`kind: "plantilla"`).
- Variable nueva `SEGMENT_SEND_ALLOWLIST`: fuera de producción real solo reciben esos números.
- Migración 0100: `customer_consents`, `segment_sends` y `segment_send_recipients` con RLS forzada.
