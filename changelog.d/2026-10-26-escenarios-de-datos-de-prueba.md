## Escenarios de datos de prueba (2026-10-26)

- HU-QA-13 (adenda de ADR-0135). `GET /api/v1/admin/qa/datos/escenarios` y
  `POST /api/v1/admin/qa/datos/escenarios/{slug}` crean, en el negocio `qa-` del tester y solo en test, datos
  deterministas y sin LLM para probar cada módulo del panel: conversación con turnos (acción con datos personales,
  consulta y traspaso entre agentes, rechazo del verificador), escalación en curso con trámites, calidad con muestra,
  versión candidata, prueba con una parte (canary al 20 %), clientes y pedidos, suscripciones, avisos por categoría,
  mensajes fallidos y cobranza completa. Los turnos corren en el ledger del kernel; las tablas de los packs las
  escribe cada pack con el gancho opcional `BaseModule.sembrar_escenario`. Repetir crea otro caso sin borrar nada.
