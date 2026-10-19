## Fuera de horario sin mensaje ya no deja al cliente sin respuesta (2026-10-18)

- Horario de atención (adenda 2026-10-18 de ADR-0051): si el negocio tiene `business_hours` pero no escribió
  `out_of_hours_message`, el cliente recibe un aviso por defecto sin LLM, en el idioma del negocio y con el nombre
  del asistente si lo tiene: «¡Hola! Ahora estamos fuera de nuestro horario de atención. Te respondemos apenas
  volvamos.». Sale una vez por conversación cada 12 h; los mensajes siguientes quedan en silencio
  (`blocked/out_of_hours`, `silent`). El texto vive en el playbook del kernel `fuera_de_horario`.
- Con mensaje configurado no cambia nada.
- API: `GET /api/v1/admin/settings` y `PUT .../identity` devuelven `identity.out_of_hours_default_message`
  (solo lectura) para que el panel muestre qué recibe el cliente.
