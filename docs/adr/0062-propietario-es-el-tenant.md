# ADR-0062 — El propietario es el tenant: el pack no duplica la identidad

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

El pack-cobranza se alineó a `app-cobranza` develop (ADR-0060) con una entidad
`pack_cobranza_propietarios` (ex-"socio") como raíz del dominio, y
`propietario_id` en `gestores`/`carteras`. Pero ADR-0059 ya fijó **1 tenant = 1
propietario**: el propietario **es** el tenant, y toda tabla del pack ya lleva
`tenant_id` (invariante nº1). La entidad `propietarios` y la columna
`propietario_id` duplican esa identidad, con el riesgo de desincronización y una
tabla/catálogo de permisos (23) que el core ya resuelve por RBAC.

## Decisión

**El propietario es el tenant.** El pack **no** tiene entidad `propietarios`:

- `pack_cobranza_propietarios` y `pack_cobranza_propietario_permisos` **se
  eliminan**.
- `gestores.propietario_id` y `carteras.propietario_id` **se eliminan**: el
  propietario de una cartera/gestor **es** el `tenant_id` (ya presente). Donde el
  código necesite "el propietario", usa `get_current_tenant_id()`.

Mapeo de lo que aportaba `propietarios`:

| Antes (pack) | Ahora |
|---|---|
| `nombre`, `apellido`, `correo`, `telefono` | owner del tenant (`Admin`/`TenantUser`) |
| `estatus` | `Tenant.status` |
| `moneda`, `pais`, `nombre_oficina_cobro`, `dias_tolerancia_cobro`, `dias_anticipacion_cobro` | `CobranzaConfig` (config del pack por tenant) |
| `propietario_permisos` (23) | RBAC del core: owner → `write`, colaborador → `read` |

`gestores` y `gestor_permisos` (14) **se quedan en el pack**: son usuarios de la
app de campo (v2), no el propietario.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Conservar `propietarios` (1 fila por tenant) | Duplica la identidad del tenant; una tabla y un catálogo de permisos para nada |
| `propietario_id = tenant_id` como columna | Columna redundante con `tenant_id`; riesgo de desincronización sin aportar nada |
| Mapear los 23 permisos a RBAC granular por acción | Alcance mayor; el core ya da owner/colaborador + grants por sección (sub-secciones = follow-up) |

## Consecuencias

**Positivas:** el pack no duplica identidad; menos tablas y menos joins; el
acceso del propietario usa el RBAC estándar del core; el alta del propietario es
el alta del tenant (ADR-0059).

**Negativas / lo que aceptamos pagar:** se pierde la granularidad de los 23
permisos por acción (owner = todo, colaborador = lectura); si se necesita, se
recupera con sub-secciones RBAC.

**Qué haría que revisáramos esto:** que el producto requiera varios propietarios
con permisos distintos bajo un mismo tenant (→ sub-secciones RBAC o sub-tenants).

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/models.py` (quitar `Propietario`,
  `PropietarioPermiso`, `propietario_id`).
- `packages/pack-cobranza/pack_cobranza/{config.py,permisos.py,admin.py,module.py}`.
- Migración `pack_cobranza/0001` (reset).
- ADR-0059/0060 (referencias al propietario como entidad del pack).
