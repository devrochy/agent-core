# ADR-0094 — Recuperación de contraseña con código de un solo uso y token de gestor desde la sesión admin

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Contexto:** HU-APP-02 (SPEC-25, solvershub-docs), decisiones D-SPEC-25-3 y ADR-APP-03 (solvershub-apk)

## Contexto

La app oficial (SPEC-25) necesita dos capacidades de auth que hoy no existen:

1. **Recuperación de contraseña self-service.** Los admins (dueños, colaboradores y gestores
   creados por el propietario) solo pueden restablecer su contraseña pidiéndoselo a otro admin
   (`POST /api/v1/admin/collaborators/{id}/password`). Sin un flujo propio, la app móvil no
   puede ofrecer "¿olvidaste tu contraseña?" y el panel web tampoco.
2. **Modo campo desde la sesión admin.** La app móvil atiende panel (token opaco de
   `AdminSession`) y campo (JWT del pack de cobranza, ADR-0065/0081) con un solo login. El core
   no puede emitir el JWT del gestor porque ningún pack depende de otro ni el core depende de
   un pack (invariante 5), y el JWT se firma con el secreto del pack por tenant.

## Decisión

**Recuperación (core):** tabla de SISTEMA `password_reset_tokens` (sin RLS, igual que
`admin_sessions`): `admin_id`, `token_hash` (SHA-256), `expires_at`, `used_at`, `created_at`.

- `POST /api/v1/auth/forgot-password {identifier}` responde **siempre 202** con el mismo cuerpo
  (no filtra si la cuenta existe). Si el admin existe y está activo: invalida los tokens
  previos sin usar, genera un código de 8 caracteres de un alfabeto sin ambigüedades
  (~40 bits), guarda su hash con expiración de 15 minutos y lo envía por correo.
- El correo sale por un **SMTP de sistema** nuevo (settings `SYSTEM_SMTP_*`, servicio
  `core/gateway/system_email.py`), no por el canal SMTP por tenant (que exige contexto de
  negocio). Sin SMTP de sistema configurado, el envío es log-only (mismo criterio que las
  alertas de Slack); el endpoint no rompe.
- `POST /api/v1/auth/reset-password {token, newPassword}`: busca por hash con
  `hmac.compare_digest`, valida expiración y uso único, aplica bcrypt y **borra todas las
  `admin_sessions` del admin** (revocación total, ADR-0025). Un intento con token inválido no
  revela si el token existió.

**Modo campo:** `POST /api/v1/pack/cobranza/gestor-token`, en la API admin del pack
(`/api/v1/pack/cobranza`, ADR-0079), autenticado con la sesión admin (`get_principal`, sin
gate de sección). Reutiliza `_membresias_de_campo` y `_sesion_de_membresia` de
`api_gestor.py`: si el admin tiene membresía de campo en el negocio activo (master,
propietario o gestor con perfil activo), emite el par access/refresh con el enforcement de
vinculación de dispositivo existente (ADR-0068). El core sigue sin conocer a los usuarios de
campo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Endpoint en el core emitiendo el JWT del gestor | El core no puede importar del pack (invariante 5); habría que duplicar la lógica de firma y secreto |
| Recuperación por link con página web propia | Exige una página de reset en el panel y manejo de URLs firmadas; el código es más simple y funciona igual en la app y en la web |
| Reusar el canal SMTP por tenant para el correo de recuperación | La recuperación ocurre sin contexto de negocio; `queue_outbound_email` exige tenant y conexión activa |
| Código de 6 dígitos | 20 bits de entropía es poco para un flujo sin rate-limit global; 8 caracteres sin ambigüedad dan ~40 bits con uso único y 15 min |

## Consecuencias

**Positivas:** recuperación usable en app y panel sin fugas de cuentas; el modo campo de la
app conserva todas las garantías del login de gestor (roles, carteras, dispositivo) sin
nuevo código de firma; una tabla de sistema sin RLS, igual criterio que `admin_sessions`.

**Negativas / lo que aceptamos pagar:** el correo de recuperación depende de configurar
`SYSTEM_SMTP_*` en los entornos (hasta entonces, log-only); el código es un secreto breve
(defendido por uso único, 15 min y hashing en BD).

**Qué haría que revisáramos esto:** que se pida recuperación por magic-link, que el pack de
cobranza migre a la identidad del core, o que aparezca abuso medible en `/auth/forgot-password`
(exigiría rate-limit por IP o captcha).

## Impacto en el código

- `core/db/models.py` (`PasswordResetToken`), `alembic/versions/0051_password_reset_tokens.py`.
- `core/admin/password_reset_service.py`, `core/gateway/system_email.py`, `core/config.py`
  (settings `SYSTEM_SMTP_*`), `core/api/routers/auth.py` (forgot/reset).
- `packages/pack-cobranza/pack_cobranza/admin_api.py` (`/gestor-token`), `docs/openapi.json`.
- App (`solvershub-apk`): pantallas de recuperación y llamado al exchange (HU-APP-02 app).
