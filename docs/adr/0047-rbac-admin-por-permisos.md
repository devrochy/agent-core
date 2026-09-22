# ADR-0047 — Autorización del panel por permisos de sección (RBAC configurable)

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

Hoy la autorización del panel se apoya en dos campos cerrados: `Admin.role`
(`master | colaborador`) y `TenantUser.role` (`owner | colaborador`). No existe forma
de clasificar admins más allá de esos dos ejes, ni de dar acceso parcial a un admin
sin volverlo `master`. Se necesita, por ejemplo, un admin de **marketing global, solo
lectura** sobre todos los tenants, y admins por tenant con secciones acotadas. El
código tiene 85 rutas que hoy solo validan "sesión válida" (salvo `_require_master` y
`_require_owner_or_master`), así que el menú oculta cosas que la ruta igual sirve: la
autorización real no es configurable ni consistente.

## Decisión

La autorización del panel se resuelve por **permisos por sección**, en una tabla de
sistema `admin_permissions`:

1. **`admin_permissions` es tabla de SISTEMA (sin `tenant_id not null`, sin RLS)**,
   con el mismo criterio que `admins`, `admin_sessions` y `tenant_user` (ADR-0012):
   se consulta al autenticar, antes de conocer el tenant. Lleva `access`
   (`read | write`) y `tenant_id` **nullable**: `null` = el permiso vale para todos
   los tenants (global); con valor = solo ese tenant. Índice único parcial sobre
   `(admin_id, section, coalesce(tenant_id, cero-uuid))`.
2. **El catálogo de secciones vive en código** (`core/admin/permissions.py`), no en
   tabla: las secciones del core (`dashboard`, `conversations`, `escalations`,
   `connections`, `plugins`, `spec`, `costs`, `plan`, `tenants`, `settings`, `admins`)
   más una por pack, derivada de `module.admin_menu`.
3. **Resolución de acceso** (de mayor a menor prioridad): `Admin.role == 'master'`
   → bypass total; si no, el permiso explícito aplicable (global o del tenant activo,
   quedándose con el máximo `write > read`); si no hay explícito, el rol del
   `tenant_user` del tenant activo como **valor por defecto** (`owner` → `write`,
   `colaborador` → `read`, solo sobre secciones de tenant). `master`, `owner` y
   `colaborador` conservan su comportamiento actual — la tabla **amplía**, no
   reemplaza, y no requiere backfill.
4. **Alcance de tenants**: `admin_tenants()` se reescribe para que un admin con algún
   permiso global (`tenant_id is null`) vea todos los tenants activos, además de los
   que tenga por `tenant_user`. Así el admin de marketing entra al panel, el
   middleware le fija un tenant (ADR-0012) y solo puede leer.
5. **Default-deny central**: el middleware de FastAPI (`tenant_context_middleware`)
   mapea ruta → sección (`required_section`, con `read` para GET y `write` para el
   resto) y deniega con 403 si no hay permiso. Cubre el panel, los routers de packs
   (`/crm`, `/calendar`) y cualquier ruta futura sin editar 85 handlers. El menú
   lateral se filtra con los mismos permisos, para que nunca muestre algo que la
   ruta va a denegar.
6. **La gestión de admins es solo de `master`** (`/admin/admins` y el CLI `admin`),
   aunque la sección `admins` sea otorgable: si no, un no-master con `admins:write`
   podría crearse un `master`, auto-concederse permisos globales o resetear la
   contraseña de un master (auto-escalada). El alcance global de un permiso
   (`tenant_id null`) y la sección `admins` solo los otorga un master.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla de roles + asignaciones (roles→permisos) | Más normalizada pero más superficie; los tres arquetipos pedidos se expresan con la misma comodidad en una tabla de grants, y `tenant_user.role` ya cubre el default por tenant. |
| Valores nuevos en `Admin.role` (`global`/`tenant`/`marketing`) | Rígido: cada sección nueva exige código; el usuario pidió RBAC configurable. |
| Permisos en `tenant.settings` (JSONB) por tenant | No expresa el alcance global (marketing sobre todos los tenants) ni se consulta antes de conocer el tenant. |
| Poner RLS a `admin_permissions` | Se consulta sin contexto de tenant (login/resolución de sesión); RLS fallaría cerrado y rompería el acceso. |
| Lista de secciones en tabla | Catálogo pequeño y ligado a rutas de código; en tabla obliga a sembrarlo y migrarlo en cada sección nueva. |

## Consecuencias

**Positivas:** un solo mecanismo para admins globales, por tenant y de marketing;
el menú y las rutas comparten la misma fuente de verdad (sin gaps de UI vs API);
los roles actuales siguen funcionando sin migración de datos; agregar una sección es
configurar un grant, no desplegar código de autorización.

**Negativas / lo que aceptamos pagar:** hay que anotar sección + `read|write` en las 85
rutas del core y en los `admin_router` de los packs; `tenant_user.role` y
`admin_permissions` conviven (dos fuentes) y hay que documentar la precedencia; sin
interfaz de gestión el RBAC es incómodo de operar, así que la página `/admin/admins` y
el CLI `agent-core admin` son parte de la decisión, no un extra. Un permiso con
alcance global (`tenant_id null`) hace que el admin vea **todos** los tenants activos
en el selector (es el punto de un admin de marketing): se acepta porque ese permiso
solo lo otorga un master y el acceso a datos sigue limitado a las secciones
concedidas (el rol de `tenant_user` no aplica a un admin sin vínculo).

**Qué haría que revisáramos esto:** que aparezcan permisos por *acción* (no por
sección) o reglas condicionales por recurso — ahí una tabla de roles/permisos normalizada
o un motor de políticas sería lo correcto.

## Impacto en el código

`core/admin/permissions.py` (nuevo), `core/admin/context.py` y
`core/admin/templates/base.html` (menú), `core/admin/views.py` (85 rutas),
`core/admin/auth.py` (contexto de permisos), `core/main.py` (gate de packs),
`core/db/models.py` + migración `0035` (`admin_permissions`, `admin_tenants()`),
`cli/commands/admin.py` (nuevo) y registro en `cli/main.py`,
`core/admin/templates/admins.html` (nuevo). Incumple el estilo del `db-migration`
para tablas de negocio **a propósito**: es tabla de sistema, excepción documentada aquí.
