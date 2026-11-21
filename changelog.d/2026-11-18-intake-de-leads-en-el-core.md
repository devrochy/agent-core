## Intake de leads en el core (2026-11-18, HU-AOS-70 parte 3)

- El formulario de la página entra por `POST /api/public/leads/{slug}` (y `GET /api/public/leads/intake-config/{slug}`) para cualquier negocio, tenga o no comercio. La ruta vieja `/api/pack/pack_commerce/…` sigue como alias. La config sale de Captar leads (`lead_settings`), no de commerce (ADR-0193 §5).
- Nueva `GET/PUT /api/v1/admin/captar-leads/config`: intake (activo, secreto, orígenes, Turnstile) y reenganche y avisos de seguimiento, con los secretos redactados.
- `pack_commerce` 4.4.0: sin rutas públicas ni salida a red; los campos de leads de su config quedan ocultos (solo los lee `copiar-leads`).
- `agent-core db copiar-leads` completa el intake de un negocio cuya config de Captar leads nació por defecto antes de la copia.
