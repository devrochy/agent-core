## Reportes de clientes: revisión master y créditos de regalo (HU-FB-04, SPEC-54, ADR-0213) (2026-11-27)

- Revisión de reportes de clientes por el equipo de SolversHub (SPEC-54, HU-FB-04, ADR-0213):
  `/api/v1/platform/feedback/reports` con bandeja entre negocios (cursor `(created_at, id)`), detalle con
  adjuntos firmados y `nota_interna`, y las decisiones `tomar`, `aprobar` con créditos de regalo y `rechazar`
  con motivo. Ver la bandeja exige actuar como master; decidir, el rol `master` estricto (el tester no decide).
- Aprobar corre en una sola transacción con el lock del saldo del negocio: topes
  `FEEDBACK_CREDITOS_MAX_REPORTE` (5.000) y `FEEDBACK_CREDITOS_MAX_MES_NEGOCIO` (10.000), transición
  condicional, abono `bonificacion` con `origen = feedback:<id>` y aviso al autor. El doble clic del mismo
  revisor responde 200 sin volver a acreditar; cualquier otra repetición, 409 `reporte_ya_decidido`.
- Aviso por correo de sistema a `FEEDBACK_AVISO_EMAIL` cuando entra un reporte (tarea
  `core.feedback.aviso_reporte_nuevo`).
- Métricas `agentcore_feedback_creditos_otorgados_total`, `agentcore_feedback_decision_seconds` y el gauge
  `agentcore_feedback_bandeja_pendientes` (función `feedback_pendientes()`, migración 0163).
