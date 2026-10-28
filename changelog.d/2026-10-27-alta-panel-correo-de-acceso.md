## Correo de acceso al crear un negocio desde el panel (2026-10-27)

- ADR-0175. `POST /admin/platform/tenants` ya no pide la contraseña del dueño: la cuenta nace sin contraseña y le llega un correo con un enlace de un solo uso para definirla y entrar (invitación `owner` de ADR-0121). `admin_password` no vacío responde 400 `admin_password_no_soportado`.
- La respuesta trae `invitation { estado, tipo, expires_at }` (`enviado`, `sin_smtp`, `fallido`, `ya_emitida`, `ya_es_miembro`); el negocio se crea aunque el correo no salga. Nuevo `POST /admin/platform/tenants/{id}/owner-invitation/resend` para reenviar el acceso.
- Una cuenta que ya existe recibe la invitación sin enlace (acepta en la app) en vez de quedar vinculada al instante; un master ya no puede ponerse a sí mismo como dueño por este camino (409 `owner_email_rejected`).
- Módulo nuevo `core/alta_acceso.py`; `core/provisioning/owner.py` delega y re-exporta. Sin migración.
