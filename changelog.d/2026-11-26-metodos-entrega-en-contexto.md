## El agente de Ventas sabe qué métodos de entrega tiene (2026-11-26, HU-CRM-28, SPEC-49)

- Referencia del turno `metodos_entrega` (solo ids y si/no) cuando el dueño apagó uno de los dos en Personalizar › Datos del cliente: el agente ya no promete «recoger en tienda» apagado antes de que `crear_pedido` lo rechace. Sale en la conversación real y en el sandbox del playground.
- Con los dos habilitados (el valor por defecto) no hay línea: el prompt queda como antes y las grabaciones de los journeys siguen valiendo. Ningún playbook cambia.
