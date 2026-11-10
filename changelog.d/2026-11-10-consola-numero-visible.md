## Número visible del WhatsApp oficial en la consola de conexiones (2026-11-10)

- `GET /api/v1/admin/connections/console` suma `display_phone_number` por conexión: el número de WhatsApp como lo sincronizó Meta (`phone_numbers.display_phone_number`), unido por `phone_number_id = external_id`. Sin llamadas a Meta; null en los demás canales o si el número aún no se sincronizó. Campo nuevo y opcional: no rompe a los clientes.
- `external_id` documenta que en WhatsApp oficial es el phone_number_id (clave de ruteo), no el número.
