# ADR-0066 — El gestor es un colaborador del core: identidad unificada

- **Estado:** aceptado
- **Fecha:** 2026-09-24
- **Ajusta:** ADR-0059 ("el gestor es un usuario interno del pack, no admin del core").

## Contexto

El pack-cobranza nació con una entidad propia `pack_cobranza_gestores` con **sus
propias credenciales** (`usuario` + `password_hash`, ADR-0060/0059), en paralelo a
los colaboradores del core (`admins` + `tenant_user`, ADR-0048). Eso da **dos
identidades y dos almacenes de credenciales** para la misma persona, y dos pantallas
que no se hablan. Además, el propietario **es** el tenant (ADR-0062), así que su
cuenta natural es un `Admin` con `tenant_user.role='owner'`.

## Decisión

**Una sola identidad**: el core.

- **Propietario** = `Admin` + `TenantUser(role='owner')`.
- **Gestor** = `Admin` + `TenantUser(role='colaborador')` **con un perfil `Gestor`**
  (el perfil es del pack: cartera asignada, los 14 permisos de la app de campo,
  `estatus`, código/IMEI). El marcador de "es gestor" **es el perfil**, no un valor
  nuevo de `TenantUser.role`.
- `pack_cobranza_gestores` gana `admin_id` (FK lógica a `admins.id`, único por tenant)
  y **pierde** `usuario` y `password_hash`.
- **Alta desde cualquiera de las dos pantallas** (una sola identidad, dos listas):
  - **Colaboradores (core)**: el pack declara su rol; al crear con ese rol, el core
    avisa al pack y este crea el perfil → aparece en Gestores.
  - **Gestores (pack)**: crear un gestor crea `Admin` + `TenantUser` + perfil →
    aparece en Colaboradores.
- **Punto de extensión del core** (aditivo, sin tabla nueva ni migración):
  `BaseModule.collaborator_roles` (declarativo) + `BaseModule.on_collaborator_role(tenant_id,
  admin_id, role_id)` (hook opcional, default no-op). Es el mismo patrón que
  `admin_menu` (ADR-0061).
- **Auth de la app**: `/auth/gestor/login` y `/auth/propietario/login` autentican
  contra `admins` (email o usuario + contraseña) + `tenant_user` (+ perfil `Gestor`
  para el gestor). El token lleva `{tenant_id, admin_id, gestor_id?, rol}`.
- **Permisos en dos capas** (se mantienen separados): las **secciones del panel**
  (RBAC del core, ADR-0047/0063) y los **14 permisos de la app de campo**
  (`GestorPermiso`, por perfil).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Nuevo `TenantUser.role='gestor'` | Cambia la resolución de RBAC del core y choca entre packs con el mismo nombre; el marcador por perfil es más simple |
| Tabla nueva en el core `tenant_user_pack_roles` | Migración y tabla de sistema para algo que el perfil del pack ya guarda |
| Core intacto y el pack reconcilia por grants de sección | No hay un "rol gestor" en Colaboradores y mezcla acceso al panel con "es gestor de campo" |

## Consecuencias

**Positivas:** una identidad y un solo login (panel y app); el propietario usa su
cuenta del core; los gestores se administran como colaboradores; el marcador es
explícito (el perfil); el core gana un punto de extensión reutilizable por otros packs.

**Negativas / lo que aceptamos pagar:** primer mecanismo **core→pack** (el hook); el
`Admin` no tiene nombre propio, así que el perfil nace con el email/usuario y se edita
en Gestores; el gestor necesita **secciones de panel** otorgadas aparte de sus 14
permisos de app; un pack deshabilitado oculta su rol del form pero deja los perfiles.

**Qué haría que revisáramos esto:** que se necesiten varios perfiles de campo por
tenant con permisos distintos, o que el hook deba ser síncrono/transaccional con el
alta del colaborador.

## Impacto en el código

- `core/mcp/types.py` (`collaborator_roles`, `on_collaborator_role`).
- `core/admin/views.py` + `core/admin/templates/colaboradores.html` (form + hook).
- `packages/pack-cobranza/pack_cobranza/models.py` + migración `0009` (`admin_id`).
- `packages/pack-cobranza/pack_cobranza/module.py` (rol + hook), `admin.py`
  (Gestores crea la cuenta), `api_gestor.py` (login contra `Admin`).
