# ADR-0175 — Acceso del dueño en el alta desde el panel: enlace por correo, sin contraseña tecleada

- **Estado:** propuesto
- **Fecha:** 2026-10-27
- **Contexto:** al crear un negocio desde el panel (`POST /api/v1/admin/platform/tenants`) no salía ningún correo: el master tecleaba la contraseña del dueño y `onboard_tenant` lo vinculaba como owner al instante. El único correo de alta vivía en el aprovisionamiento automático (ADR-0121 §3.8).
- **Usa:** ADR-0121 (invitación `owner` con enlace de un solo uso y canje que define la contraseña), ADR-0124 (el canje abre la sesión), ADR-0081 (invitaciones: nadie queda miembro de un negocio sin aceptar), ADR-0098 (correo de sistema por SMTP) y ADR-0134 (el tester opera como master, pero solo con cuentas nuevas).
- **Amplía:** ADR-0121 §3.8: el mismo mecanismo ahora sirve al alta hecha por una persona.

## Contexto

- Un master que da de alta un negocio comunicaba la contraseña por otro canal. No había aviso al dueño, ni enlace, ni forma de saber si le llegó.
- El enlace de ADR-0121 solo sirve a una cuenta **limpia** (sin contraseña, sin acceso a otro negocio, sin otras invitaciones pendientes): `canjear_invitacion` lo exige y crea la membresía al canjear. Vincular al dueño antes (como hacía `onboard_tenant`) vuelve imposible el canje (`YA_PERTENECE`).
- El SMTP de sistema ya funciona en test (recuperación de contraseña, invitaciones de tester).

## Decisión

**El panel ya no define la contraseña del dueño: la cuenta nace sin contraseña y le llega un correo con un enlace de un solo uso.**

1. `POST /admin/platform/tenants`:
   - `admin_password` no vacío → 400 `admin_password_no_soportado` (vacío sigue válido). El panel deja de pedirla.
   - Orden: validar → comprobar que el correo se puede invitar (no es master, tester, cuenta con permisos globales ni cuenta desactivada: 409 `owner_email_rejected` **antes** de crear nada) → crear la cuenta sin contraseña → `onboard_tenant` **sin** `admin_email` (no vincula) → emitir la invitación `owner` → confirmar → enviar el correo → auditar.
   - La respuesta agrega `invitation { estado: enviado | sin_smtp | fallido | ya_emitida | ya_es_miembro, tipo: enlace | app, expires_at }`. **Nunca** lleva el enlace ni el token.
2. **El negocio se crea aunque el correo no salga.** El envío es posterior al commit, con tope de espera de 10 s. `sin_smtp` (sin `SYSTEM_SMTP_*`) y `fallido` se informan en la respuesta; no hay outbox ni estado de entrega persistido.
3. **Reenvío explícito:** `POST /admin/platform/tenants/{tenant_id}/owner-invitation/resend`. Actúa solo sobre la invitación `owner` pendiente de ese negocio y envía a la cuenta de esa invitación (el destinatario no viene en la petición). Reusa el enlace mientras no venza y lo rota (UPDATE condicional sobre el nonce) si venció; si la cuenta dejó de estar limpia, anula el enlace y manda la variante «app». 404 sin negocio; 409 `tenant_inactivo` o `sin_invitacion_pendiente`.
4. **Cuenta que ya existe** (con contraseña o con acceso a otro negocio): invitación sin enlace; el correo manda al panel a aceptar. No hay `TenantUser` hasta que acepte. Es un cambio para el master, que antes vinculaba al instante.
5. **Idempotencia:** sin migración. Se envía solo si esta llamada **emitió** algo (creó la pendiente o le dio su nonce, que se fija una sola vez); un doble POST responde `ya_emitida` y no repite el correo. El par `(tenant, admin)` pendiente es único (`uq_tenant_invitation_pending`); si dos peticiones compiten, la perdedora relee.
6. **Un tester** (ADR-0134) sigue limitado a negocios con slug nuevo y cuenta nueva; se vuelve a comprobar con el resultado de crear la cuenta (el pre-check es previo y por tanto no es atómico).
7. **Módulo:** `core/alta_acceso.py` (hermano de `core/alta_negocio.py`). `core/provisioning/owner.py` le delega `InvitacionOwner`, `asunto_y_cuerpo` y `VIGENCIA_ENLACE` y los re-exporta; `correo_valido` se mueve desde el consumidor. Está prohibido para las tools, el host MCP y el LLM (`.importlinter` y `tests/test_provisioning_sin_llm.py`).
8. **Observabilidad:** eventos `tenant_owner_access_sent`/`_failed`/`_resend`/`_resend_rejected` con `tenant_id`, `invitation_id`, correo enmascarado y duración del envío; sin token, enlace ni cuerpo. Auditoría `tenant.owner_access` bajo el `tenant_scope` del negocio.

## Consecuencias

- Un master ya no puede dejarse a sí mismo como dueño por este camino (409): se invita desde Colaboradores.
- No hay cooldown de reenvío ni estado visible de «acceso pendiente» en el listado. Si hace falta, una columna `last_sent_at` anulable en `tenant_invitations` lo resuelve sin tocar el contrato.
- Si el proceso muere entre el commit y el envío, el reintento del POST no reenvía solo (`ya_emitida`): se usa «Reenviar acceso». Una cuenta recién creada sin negocio queda huérfana pero inerte, y el tester no puede reutilizar ese correo.
- El timeout cancela la espera, no el hilo del SMTP: un correo reportado como `fallido` puede haber salido (el reenvío manda el mismo enlace).
- **Abierto (auditoría multitenant, media):** `resend` exige master o tester habilitado, y nada liga a un tester con los negocios que creó, así que un tester puede reenviar el acceso de cualquier negocio (el destinatario no cambia, pero cada llamada manda un correo y, si la cuenta ya no está limpia, anula el enlace vigente). Falta un enfriamiento por invitación y, si se decide, limitar al tester a los negocios que creó (requiere guardar el creador).
- El admin HTML heredado (`core/admin/views.py`) y la CLI siguen creando con contraseña y sin correo hasta su retiro (ADR-0076).
- Producción: `PANEL_BASE_URL` debe apuntar al panel; si falta, `url_del_panel()` cae en `PUBLIC_BASE_URL` y el enlace saldría al host de la API.
