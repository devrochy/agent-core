## Sumas por etapa para el tablero de embudo (HU-CRM-24, 2026-11-03)

- **`GET /api/v1/admin/leads` acepta `resumen=valor`** junto a `facetas`: cada faceta devuelve además `sumas`, el valor
  estimado que suman los leads de cada opción (`sin_valor` incluido, `0` si no hay). Las sumas respetan los mismos filtros,
  agente y búsqueda que los contadores, y un lead sin valor estimado aporta `0`. Sin `resumen` la respuesta no cambia
  (`sumas` queda en `null`). Un valor de `resumen` desconocido se rechaza con 422.
- **Registro de leads.** Nuevo gancho opcional `medida_query(medida)` en `LeadProvider`: un `SELECT customer_id, monto` por
  cliente. El core lo une a `facet_query` sin conocer las tablas del pack; `pack_commerce` lo implementa para `valor`.
- Sin migración ni cambios en el esquema de tools: las grabaciones del LLM siguen vigentes.
