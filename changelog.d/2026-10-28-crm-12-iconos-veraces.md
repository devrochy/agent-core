## HU-CRM-12 · iconos veraces y filtro por agente en Clientes (backend)

- `LeadRelacion` en el `lead_registry` (ADR-0179): un pack declara `con_datos(ids)`, el SQL que define «este cliente tiene datos reales del módulo». Commerce = pedido, cotización o lead; Agenda = cita no borrada; Suscripciones = suscripción no borrada; Cobranza = cliente de cartera enlazado. `ficha` y `detalle` quedan declaradas para HU-CRM-14.
- La columna `agentes` de `GET /admin/leads` y de su CSV deja de mentir: Ventas ya no sale en todos los clientes (el resolver devolvía «Nuevo» y 0 pedidos a cualquiera). Con `relacion` manda `con_datos`; sin ella se conserva la heurística de las columnas. General sigue siempre presente. `agents[]` no cambia.
- `agente=<key>` (aditivo) en `GET /admin/leads` y `GET /admin/leads/export.csv`: solo los clientes con datos de ese agente. `general` no filtra; una clave desconocida, de un pack no habilitado o sin `con_datos` responde 400 `filtro_invalido`.
