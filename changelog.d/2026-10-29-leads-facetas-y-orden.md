## Clientes de Ventas: contadores por etapa, orden y última actividad en `GET /admin/leads` (2026-10-29)

- **HU-CRM-17 (lectura):** la lista de clientes acepta `facetas=<clave>` (repetible) y responde, por cada una,
  cuántos clientes hay por opción (`counts`), cuántos no tienen valor (`sin_valor`) y el `total`. Se calcula con
  una sola consulta agrupada por clave, respeta la búsqueda y los demás filtros pero no el propio (las pestañas
  de un mismo filtro suman contra el mismo total). Una clave sin contadores responde 400 `filtro_invalido`.
- **Orden:** `orden=alta|nombre|nombre_desc|actividad` (por defecto `alta`, como hasta hoy). `actividad` ordena
  por el último mensaje de cualquiera de sus conversaciones y deja al final a quien nunca escribió. Un valor
  fuera de la lista responde 422.
- **Fila:** cada cliente trae `ultima_actividad` (hora del último mensaje o `null`).
- **Contrato de packs:** `lead_registry.register(..., facet_query=...)` permite que un pack declare cómo contar
  sus filtros sin que el core conozca sus tablas. `pack_commerce` lo declara para `estado_lead` y
  `clasificacion`.
- Compatible hacia atrás: sin los parámetros nuevos la respuesta solo suma `facetas: []` y `ultima_actividad`.
  No hay migración.
