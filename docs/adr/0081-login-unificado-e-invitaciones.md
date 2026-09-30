# ADR-0081 — Identidad unificada de acceso: login por credenciales, selección de negocio y membresía por invitación

- **Estado:** aceptado
- **Fecha:** 2026-09-29
- **Modifica:** ADR-0057, ADR-0059, ADR-0062, ADR-0065, ADR-0066, ADR-0077 (superficie de autenticación de la API de campo y membresía multi-negocio).

## Contexto

La API de campo del gestor (`pack_cobranza/api_gestor.py`, ADR-0065) fue migrada desde
el backend NestJS aceptando el contrato exacto de la APK, con una adición: el login
**exige `tenant`** (slug), porque agent-core es multi-tenant. La APK lo envía desde
`EXPO_PUBLIC_TENANT`, compilado en el bundle (`solvershub-apk/src/config.ts`,
`docs/ai/tasks/migracion-api-agent-core.md`).

Eso deja cuatro problemas abiertos que este ADR resuelve:

1. **La APK queda atada a un solo tenant en compilación.** No puede servir a un usuario
   que pertenece a más de un negocio ni distribuirse como una sola app.
2. **El modelo ya soporta multi-membresía pero nadie la usa.** `TenantUser` es único por
   `(tenant_id, admin_id)` (`core/db/models.py:73-84`), `Admin.email`/`username` son
   únicos globales (`:425-426`), y `Gestor` es único por `(tenant_id, admin_id)`
   (`pack_cobranza/models.py:79`). Es decir, un mismo `Admin` puede ser gestor/propietario
   en varios tenants.
3. **El master no entra a la API de campo.** `admin_tenants()` ya le devuelve todos los
   tenants activos (ADR-0047, migración 0037), pero el login del pack lo rechaza (no tiene
   perfil `Gestor`) y no hay rol de acceso total salvo `"propietario"`
   (`api_gestor.py:328-351`).
4. **Alta de identidades duplicada y sin consentimiento.** Tanto el core
   (`collaborators_service.create_collaborator`) como el pack
   (`admin_api.create_collector`, `admin.py::_save`) **crean un `Admin` nuevo y fallan si
   el correo ya existe**. No se puede "asignar una persona existente a un negocio", y
   tampoco hay consentimiento: un owner podría meter a cualquiera en su tenant.

## Decisión

### 1. Login único por credenciales (sin slug ni rol)

- `POST /auth/login {usuario, password, [tenant], [imei], [whatsapp]}` en el router
  público del pack. `/auth/gestor/login` y `/auth/propietario/login` quedan como
  **alias de compatibilidad** del mismo contrato.
- El login de **dispositivo v1** (código + API key) pasa de `POST /auth/login` a
  `POST /auth/dispositivo` para liberar la ruta sin sombras (no tenía consumidores:
  la APK migró al contrato de ADR-0065; solo lo citaba la documentación).
- La credencial identifica un `Admin` global (email o usuario). El login **no** pide rol:
  el rol es una propiedad de la **membresía**, no de la cuenta.
- Se resuelven las membresías con `admin_tenants(admin_id)` (SECURITY DEFINER, sin RLS),
  filtrando a tenants **activos con `pack_cobranza` habilitado**. El rol de campo por
  negocio se deriva así:
  - `role = 'global'` (master o permiso global) → **master** (acceso total).
  - `tenant_user.role = 'owner'` → **propietario** (acceso total).
  - colaborador con perfil `Gestor` activo → **gestor** (scopeado a sus carteras).
  - colaborador sin perfil de campo → **no es usuario de la API de campo** (usa el panel).

### 2. Selección de negocio

- Sin `tenant`: **0** membresías → `401`; **1** → tokens de ese negocio; **N** →
  `{seleccionRequerida: true, negocios: [{slug, nombre, rol}]}` sin tokens.
- Con `tenant`: se valida que sea membresía del admin y se emiten los tokens.
- El JWT del pack es **por-negocio** (firmado con el secreto de ESE tenant,
  `pack_cobranza/auth.py:94-118`), así que la app obtiene los tokens definitivos
  **re-autenticando con el negocio elegido** (misma UX de un solo login). No se inventa un
  token multi-negocio firmado con una clave nueva.

### 3. Acceso total para master y propietario en la API de campo

- Se generaliza la condición de acceso total: `rol != "gestor"` ⇒ todas las carteras y
  todos los permisos (`_carteras_visibles`/`_permisos_de`). El token de un master/owner no
  lleva `gestor_id`.
- Las escrituras que hoy asumen `gestor_id` deben aceptar `None` o derivarlo de la cartera.
- La vinculación de dispositivo (HU-39/43, ADR-0068) **no aplica** a tokens de acceso
  total (no hay perfil `Gestor`).

### 4. Membresía por invitación con consentimiento

- Nueva tabla de sistema **`tenant_invitations`** (sin RLS, patrón `tenant_user`,
  ADR-0012): el invitado debe poder ver la invitación **antes** de tener acceso al tenant.
  Acceso por funciones/métodos SECURITY DEFINER.
- Alta de colaborador/gestor:
  - **Identidad nueva** (email/usuario no existe) → se crea `Admin` y se vincula directo
    (comportamiento actual).
  - **Identidad existente** → **no** se vincula ni se toca su contraseña: se crea una
    **invitación pendiente** que el usuario debe aceptar.
- Estados: `pendiente | aceptada | rechazada | revocada`. Al **aceptar** se crea el
  `TenantUser` y, si aplica, el perfil `Gestor` (hook `on_collaborator_role`, ADR-0066).
- Un **owner** puede invitar a su negocio (hoy `link_tenant` es solo-master), con guardas:
  no se invita a masters ni a cuentas con permisos globales; todo queda auditado.
- Aviso **solo in-app** (badge/listado); sin canales externos en esta versión.

### 5. Token de membresía (invitaciones sin negocio activo)

- Un usuario con **0 membresías activas pero ≥1 invitación pendiente** no puede recibir un
  JWT firmado por un tenant. El login le emite un **token de membresía** firmado con
  `APP_SECRET_KEY` y **limitado a `/auth/invitaciones/*`** (listar, aceptar, rechazar).
- Tras aceptar, la app re-loguea para obtener los tokens de negocio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `EXPO_PUBLIC_TENANT` por build (status quo) | Un APK por cliente; no sirve multi-membresía ni se puede distribuir uno solo |
| Tenant como campo de texto en el login | El usuario no conoce el slug; mala UX y filtración de identificadores internos |
| Resolver tenant por email sin listar membresías | No cubre usuarios en varios negocios ni permite elegir; ambigüedad silenciosa |
| Token multi-tenant firmado con clave global | Rompe el aislamiento por-tenant del secreto del pack (ADR-0057) y mezcla realms |
| Vincular directo al asignar existente | Sin consentimiento: un owner metería a cualquiera en su negocio |
| Aceptar invitaciones solo en el panel | El usuario es de campo; el objetivo es hacerlo desde la APK |

## Consecuencias

**Positivas:** una sola APK para todos los negocios; el usuario de campo con acceso a
varios negocios elige dónde entrar; el master puede operar la app de campo con acceso
total; reutilizar identidad elimina cuentas duplicadas; el vínculo a un negocio requiere
consentimiento explícito y queda auditado.

**Negativas / lo que aceptamos pagar:** el login del pack deja de ser por-tenant puro
(resuelve membresías cross-tenant, siempre filtradas por `admin_tenants`); un re-login
extra al elegir negocio; nueva tabla de sistema exenta de RLS; el token de membresía
introduce un segundo tipo de token (limitado por scope); hay escrituras de la API de campo
que deben aceptar `gestor_id = None`.

**Qué haría que revisáramos esto:** requisitos de atestación de dispositivo para
master/owner, invitaciones con enlace externo (WhatsApp/email), o varios propietarios con
permisos distintos bajo un mismo tenant.

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/api_gestor.py` (login unificado, acceso total,
  endpoints de invitaciones), `auth.py` (token de membresía), `docs/api-gestor.md`.
- `core/db/models.py` + migración alembic (`tenant_invitations`) y funciones SECURITY
  DEFINER; `core/admin/invitations_service.py`; `core/api/routers/invitations.py`.
- `core/admin/collaborators_service.py` y `pack_cobranza/admin_api.py`/`admin.py` (invitar
  si la identidad existe; invitación por owner).
- `solvershub-apk/src/{config.ts, api/auth.ts, types/auth.ts, session/*, screens/*}` y
  `solvershub-panel/apps/panel-web/app/(app)/colaboradores` y `cobranza/gestores`.
- `docs/openapi.json` (rutas nuevas) y ADR-0059/0062/0065/0066 (nota de modificación).
