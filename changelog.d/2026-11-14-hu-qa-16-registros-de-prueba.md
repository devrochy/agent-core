## Lo cargado para probar el agente, en palabras (HU-QA-16, SPEC-45, 2026-11-14)

El asistente de crear agentes muestra la lista de lo que se generó o importó para tenerla en cuenta al
probar el agente. Hasta ahora la API solo devolvía conteos.

- **Hook nuevo y opcional `describir_registros(tenant_id, registros)`** en `BaseModule` (ADR-0194, en
  curso), con el tipo `RegistroDescrito` en `core/qa/contrato_datos.py`. Por lote, solo lectura, bajo
  `tenant_scope` y filtrando por `tenant_id`. Lo implementan:
  - `pack_commerce`: productos con precio (el menor de sus variantes activas, «desde» si varían) y categoría.
  - `pack_calendar`: servicios (tipos de cita) con su duración. Citas y bloqueos no salen como filas.
  - `pack_cobranza`: clientes con su cartera, valor y saldo de sus préstamos vivos.
  - `pack_subscriptions`: planes con precio, periodicidad y días de prueba; suscripciones con el nombre del
    cliente, su plan y su estado. Clientes y cargos no salen como filas.
- **API (aditiva):** `GET /api/v1/admin/agents/{agent_id}/datos-de-prueba/registros` →
  `RegistrosDePrueba` (`lote`, `registros[]` con `tipo`/`nombre`/`detalle`/`origen`, `total`,
  `sin_detalle`). Solo con `QA_TESTERS_ENABLED` (404 sin ella) y sección `agents`, como el `GET` del
  estado. Trae el último lote generado más lo importado vivo; lo de un módulo cuya sección el usuario
  no ve no sale. Hasta 200 filas. `LoteGenerado` no cambia.
- **Despliegue:** sin migración.
