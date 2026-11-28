## Reportes de clientes: envío con evidencia (HU-FB-03, SPEC-54, ADR-0213) (2026-11-27)

- Migración 0162: tablas `feedback_reports` y `feedback_attachments` con RLS forzada, CHECKs de la decisión,
  trigger que deja inmutable un reporte aprobado o rechazado, sin borrado (`prevent_mutation`) y dos funciones
  `SECURITY DEFINER` de solo lectura para la bandeja del master (`feedback_bandeja`, `feedback_reporte_tenant`).
- `POST /api/v1/feedback/reports` (multipart, `Idempotency-Key` obligatoria; 201, o 200 en un replay),
  `GET /api/v1/feedback/reports`, `GET /api/v1/feedback/reports/{id}` y `GET /api/v1/feedback/catalogo`. Cualquier
  rol del negocio envía; cada quien ve lo suyo y el owner todo el negocio con `alcance=negocio`.
- Perfil de almacenamiento `feedback` (imágenes y PDF, 8 MB cada uno; máximo 5 y 25 MB por envío). Cupo de 5 por
  hora por usuario y 30 por día por negocio (429 con `Retry-After`).
- Todo detrás de `FEEDBACK_ENABLED` (apagada por defecto). `ApiError` admite cabeceras. Nueva entidad de
  notificación `feedback_reporte`.
