## Instagram con Facebook Login: salida y alta (ADR-0152, HU-CHAN-04) (2026-10-23)

Entrega 4a del rescate de Instagram: cuentas profesionales vinculadas a una Página, con la ruta de Facebook Login.
`instagram_meta` pasa a `available = true`. La ruta por Instagram Login (otra app, token de 60 días con refresco) y la
prueba de humo con diagnóstico quedan para la entrega 4b.

- **Salida** (`channels/instagram/`): `POST /{page_id}/messages` con el token de la Página vinculada y el IGSID como
  destinatario; ventana de 24 h y `HUMAN_AGENT` como en Messenger; texto de hasta 1000 bytes; sin imágenes todavía.
  Las credenciales salen de la cuenta (`ig_accounts`) y de la custodia de tokens, con las mismas defensas que
  Messenger (pausa y borrado de la cuenta, negocio explícito, sin respaldo a otra cuenta ni al `.env`).
- **Alta** (`/api/v1/admin/meta/instagram`: `GET`, `POST /attempt`, `POST /discover`, `POST /connect`; propietario o
  master): lista las cuentas de Instagram de las Páginas que la persona administra y conecta las elegidas, con una
  cuenta = una conexión canónica. Una cuenta pertenece a un solo negocio. Contrato en `docs/openapi.json`.
- **Clientes por cuenta:** la clave del cliente de Instagram es `instagram:<ig_user_id>:<IGSID>`; el mismo IGSID en
  dos cuentas del negocio no mezcla conversaciones.
- **Suscripción sin pisar Messenger:** se piden la unión de los campos ya suscritos de la Página y los de Instagram.
- **Nueva variable `META_INSTAGRAM_CONFIG_ID`** (propia, sin respaldo en las de Messenger y WhatsApp). Sin ella el alta
  responde `meta_configured = false`. **Hay que crear la Login Configuration en Meta antes de probar en test.**
- Plan: Instagram ya estaba en los planes de pago (decisión del dueño); Free ve el motivo.
- El panel clásico muestra Instagram deshabilitado con «Solo en el panel nuevo» (la pantalla vive en solvershub-panel).
- **Corrección también para Messenger (auditoría):** su suscripción ya no reemplaza los campos de la Página (pide la unión,
  como Instagram); el token de la Página/cuenta se guarda después del reclamo y no antes; y un identificador con una
  conexión manual del mismo negocio se omite con `manual_connection_conflict` en vez de quedar «conectado» sin responder
  por la Página.
