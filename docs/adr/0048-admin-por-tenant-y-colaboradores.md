# ADR-0048 — Administrador por tenant, colaboradores y alta con credenciales

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

Hasta ahora `admins` es global y el panel se entra con un admin de plataforma que ve
los tenants a los que esté vinculado en `tenant_user`. No hay forma de darle a un
tenant un **login propio**, ni una pantalla para que el negocio administre sus
propias cuentas. El alta de tenant (`onboard_tenant`) solo **vincula** un admin
preexistente (`admin_email`), nunca lo crea, así que un tenant dado de alta sin
vincular a nadie queda sin forma de entrar. Se quiere, además, una sección de
**Colaboradores** al estilo de agent-sale (crear cuentas, roles, permisos,
activar/desactivar) y que el alta de tenant pida credenciales.

## Decisión

1. **El administrador de un tenant es un `Admin` normal (`role='colaborador'`) con
   `TenantUser.role='owner'`.** No es `master` de plataforma: loguea con email +
   contraseña y `admin_tenants()` le devuelve solo su(s) tenant(s). "Master" sigue
   siendo el superusuario de plataforma.
2. **Sección RBAC `collaborators`** (reemplaza a `admins`). Categorías:
   - `GLOBAL_ONLY = {tenants}` (master o permiso explícito).
   - `OWNER_ONLY = {collaborators}`: el `owner` la tiene; el `colaborador` **no**
     (gestionar cuentas no es capacidad de colaborador).
   - El resto: `owner` → write, `colaborador` → read.
3. **Una sola página "Colaboradores"** en `/admin/colaboradores` (con redirect
   desde `/admin/admins`), adaptativa por rol:
   - **master**: todos los admins/tenants; crea admins globales, vincula tenants,
     otorga permisos globales.
   - **owner**: solo las cuentas de su tenant activo; crea colaboradores, edita rol,
     otorga permisos **del tenant** (nunca globales ni `tenants`/`collaborators`),
     activa/desactiva, resetea contraseña.
   - Guards anti-escalada: un no-master no crea master de plataforma, no toca
     cuentas de otro tenant, no se auto-desactiva.
4. **Alta con credenciales.** `onboard_tenant(..., admin_password=None)`: con
   contraseña, **crea** el admin del tenant (si el email no existe) y lo vincula como
   `owner`; sin contraseña, mantiene el comportamiento histórico (vincular
   preexistente). El wizard `/admin/tenants/nuevo` agrega el paso "Administrador del
   negocio" (email + contraseña) y el master que crea el tenant queda vinculado
   igual, para no perderlo del selector.
5. **CLI `agent-core tenant admins`**: crea un admin `owner` por tenant de forma
   idempotente (por defecto solo a los que no tienen; `--incluir-con-admin` a todos)
   con email `<slug>@<dominio>` y contraseña aleatoria, y **devuelve la lista**
   (tabla + CSV opcional). El email generado usa `@gmail.com` por decisión del
   operador — se asume el riesgo de ambigüedad con cuentas reales.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Hacer que el admin del tenant sea `master` de plataforma | Vería y tocaría todos los tenants; rompe el aislamiento. |
| Tabla `admins` con `tenant_id` y RLS (modelo agent-sale 0033) | agent-core ya resolvió admin↔tenant con `tenant_user` (ADR-0012) y el RBAC con `admin_permissions`; migrar a admin-por-tenant con RLS duplicaría el mecanismo y rompería el login global del operador. |
| Renombrar `owner` → `master` en `tenant_user` | Churn de datos/tests sin beneficio funcional; "master" queda reservado al superusuario de plataforma. |
| Generar contraseña y mostrarla en un flash (URL) | La contraseña quedaría en logs/historial; el wizard la pide explícita y el CLI la imprime una sola vez en consola. |
| Dominio reservado (`@tenants.agentcore.local`) | El operador eligió `@gmail.com`; se documenta el riesgo. |

## Consecuencias

**Positivas:** cada tenant puede tener login propio sin ser superusuario; el negocio
administra sus cuentas y permisos; el alta ya no deja tenants inaccesibles; una sola
pantalla de cuentas en vez de dos.

**Negativas / lo que aceptamos pagar:** `admins` sigue siendo una tabla global (el
aislamiento depende de `tenant_user` + el gate, no de RLS en `admins`); el email
`<slug>@gmail.com` puede colisionar con una cuenta real y no recibe correo; el wizard
obliga a elegir contraseña (no la genera) para no filtrarla por la URL.

**Qué haría que revisáramos esto:** que se necesiten admins pertenecientes a varios
tenants a la vez con roles distintos por tenant, o auto-registro/recuperación de
contraseña por email — ahí conviene el modelo con RLS de agent-sale (0033) y una
tabla de tokens de reset (0055).

## Impacto en el código

`core/admin/permissions.py` (secciones), `core/admin/views.py` (página Colaboradores),
`core/admin/templates/{colaboradores,base,tenants_new}.html`, `core/onboarding.py`
(`admin_password`), `cli/commands/tenant.py` (`create` + `admins`), `core/main.py`
(mapa de rutas), `docs/adr/README.md`, `CLAUDE.md`. Reemplaza la sección `admins`
introducida en ADR-0047 (que sigue vigente para el modelo de permisos).
