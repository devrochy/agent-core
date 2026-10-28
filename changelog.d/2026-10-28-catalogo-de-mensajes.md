## Catálogo unificado de mensajes, de solo lectura (2026-10-28)

- `GET /api/v1/mensajes/catalogo` lista juntos, con una forma común y un puntero al editor de origen, los mensajes del negocio: plantillas de Meta, avisos por evento, plantillas con opciones y textos de los packs. Cada tipo solo se incluye si quien consulta puede leer su sección (`connections`, `settings`, `plugins`); lo omitido se informa en `omitidas`. No migra datos, no cambia comportamiento y no sincroniza con Meta.
- Los packs marcan sus campos de mensaje con `json_schema_extra={"mensaje": "<canal>"}` (calendario, suscripciones y comercio); el core no conoce sus nombres. Un test falla si un campo nuevo con `{variables}` queda sin marcar.
- Servicio `core/admin/catalogo_mensajes_service.py`; `canales_de(tipo, marca)` es el único punto que sustituirá la tabla de capacidades por canal (pista C1).
- ADR-0178 (propuesto) en solvershub-docs.
