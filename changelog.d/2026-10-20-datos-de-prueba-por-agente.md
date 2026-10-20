## Datos de prueba por agente según la descripción del negocio (2026-10-20)

- En test (`QA_TESTERS_ENABLED`), `GET/POST/DELETE /api/v1/admin/agents/{id}/datos-de-prueba` genera
  con el LLM el catálogo, los planes, la agenda o la cartera del agente según la descripción del
  negocio, con la plantilla del pack como respaldo; regenerar reemplaza y borrar retira solo lo sembrado
  (HU-QA-10, ADR-0146).
- Migración 0098: tabla `test_data_records` (RLS forzada) y `usage_ledger.kind = 'generacion'`.
- Los seeds de commerce, calendar y cobranza informan lo que crean; commerce ya no reutiliza una
  categoría borrada.
