## «Preparar mis datos» deja el negocio de prueba listo para el Agent OS (HU-QA-09, 2026-10-18)

- Centro de pruebas (solo con `QA_TESTERS_ENABLED` y `AOS_CONNECTION_ROUTING`): el negocio `qa-` del tester queda
  con chat web activo (clave y enlace `/w/<clave>` de la landing), backfills hechos, `aos_runtime = v3`,
  `aos_procedures`, los agentes General, Ventas y Agenda en el chat web con las reglas del Coordinador,
  la release activa y `aos_routing = active`. Adenda de ADR-0135.
- `POST /admin/qa/datos` con los datos ya listos responde 200 y solo pone al día el Agent OS (antes 409); la
  respuesta y `GET /admin/qa/datos` traen `agent_os` (estado, motivo, chat web y agentes).
- `chat_web_conexion.guardar_widget`: la escritura del widget, compartida por Configuración y el centro de pruebas.
- El negocio de prueba trae el plan `QA-PLAN-MENSUAL` (150.000 COP mensual) para `contratar_plan`; volver a pulsar
  lo agrega a los negocios de prueba anteriores. Sin cobro en línea (no se copian claves de Wompi).
