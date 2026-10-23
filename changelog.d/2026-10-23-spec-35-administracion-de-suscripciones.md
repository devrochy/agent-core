## Administración de suscripciones en la API (2026-10-23)

- SPEC-35 (HU-SUB-08/19/20/16). `pack_subscriptions` expone lectura de suscripciones (lista con filtros y detalle con
  cargos, historial y acciones válidas), pagos (cargos, con mora), clientes con suscripción y métricas (MRR, altas,
  bajas, churn, mora por antigüedad y cohortes por plan).
- Acciones `pausar`, `reanudar` (nueva en la API), `cancelar` y `reenviar-link`, con confirmación de estado en el
  servidor y fila de `audit_log` con el usuario en la misma transacción. Las tools del LLM no cambian ni duplican auditoría.
- Reenviar link encola el recordatorio por el outbox con clave de aviso propia (`sub_resend:`); sin WhatsApp devuelve el link.
- `LeadProvider` del pack: columnas, filtro por estado de suscripción (`mora`, `ninguna`) y línea de tiempo en Clientes.
- Migración `0008`: índices por `estado` y `fecha_vencimiento`. `admin_menu` apunta a `/suscripciones`.
- Enmienda de ADR-0106. Pruebas: `tests/integration/test_subscriptions_admin_ui.py` (aislamiento, permisos, auditoría).
