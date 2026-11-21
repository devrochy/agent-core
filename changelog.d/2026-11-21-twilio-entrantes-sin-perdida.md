## WhatsApp vía Twilio: ningún entrante se pierde sin rastro (2026-11-21)

- Un mensaje de cliente que la conexión no procesa queda en la auditoría de webhooks con su motivo y avisa una vez por hora. Los motivos son: conexión pausada, negocio no operativo, número de destino ajeno o mensaje sin contenido. Antes respondía 200 sin dejar rastro (incidente del 2026-11-19: entrantes de producción caían en la conexión pausada de test).
- Cron nuevo `core.twilio_reconciliacion`, cada 2 min: trae de la API de Twilio los entrantes de los últimos 30 min que no llegaron por el webhook (despliegue, túnel caído, número apuntando a otra URL) y los publica por el mismo camino, sin duplicar. Solo actúa sobre senders conectados y activos. Si recupera algo, avisa.
- Adenda del ADR-0186 (2026-11-21). Sin cambios de esquema ni de la API `/api/v1`.
