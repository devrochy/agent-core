## Aviso permanente de plan Free (2026-11-12)

- HU-BILL-15. `GET /api/v1/admin/me` incluye `plan` con `plan_id`, `plan_name`, `status`, `days_left`, `trial_ends_at` y `has_plan`. Sale de la suscripción activa del negocio, sin consultar consumo ni estado operativo, para que el panel y la app muestren el recordatorio de upgrade sin otra petición.
- Nuevo `core.billing.enforcement.plan_summary()`: lectura barata y sin efectos secundarios de `tenant_subscriptions` + catálogo de planes.
- `docs/openapi.json` regenerado con `MePlanSummary`.
