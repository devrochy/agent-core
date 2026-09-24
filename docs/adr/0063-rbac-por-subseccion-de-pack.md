# ADR-0063 — RBAC por sub-sección de pack

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

El RBAC del panel (ADR-0047) da **una sección por pack** (el `plugin_id`). Un
colaborador del tenant puede tener `read`/`write` sobre el pack entero, pero no
distinguir áreas: no hay forma de darle escritura sobre "Carteras" y solo lectura
sobre "Gastos". El panel de referencia (`app-cobranza-admin`) sí distingue
permisos por área. Con packs grandes (cobranza) un único permiso es demasiado
grueso.

## Decisión

Los items de `admin_menu` pueden declarar **`section_key`** (por defecto, el
`plugin_id`). El RBAC pasa a resolver por **sub-sección**:

1. `pack_prefixes(modules)` mapea **cada path de item** a su `section_key` (no ya
   el primer segmento al `plugin_id`).
2. El catálogo de secciones (`catalog`) incluye las sub-secciones de los packs
   activos; `permissions_for` las resuelve igual que cualquier sección (rol por
   defecto + grants explícitos en `admin_permissions`).
3. El middleware (`required_section`) y el handler (`require_section`) resuelven
   la sub-sección por el **path** (prefijo más largo).
4. El menú (`base.html`) muestra cada item solo si el admin tiene acceso a **su**
   sub-sección; el grupo aparece si queda algún item visible.

Así un colaborador puede tener `write` en `pack_cobranza:carteras` y `read` en
`pack_cobranza:gastos`, vía grants explícitos.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Una sección por pack (status quo) | Granularidad insuficiente; no permite mezclar read/write por área |
| Una sección por ruta | Explosión de permisos; cada ruta sería un permiso |
| Permisos por acción (como la matriz de 23 del panel de referencia) | Más fino que lo que el core necesita; sub-sección cubre el caso con menos piezas |

## Consecuencias

**Positivas:** granularidad por área sin tocar el modelo de datos; reutilizable
por cualquier pack; el menú refleja el permiso real.

**Negativas / lo que aceptamos pagar:** más secciones en el catálogo (una por
item de menú); un pack con muchos items genera muchas sub-secciones; el
`section_key` debe mantenerse coherente con el `path`.

**Qué haría que revisáramos esto:** que un pack necesite permisos por acción (→
matriz explícita) o que el número de sub-secciones se vuelva inmanejable (→
agrupar por secciones de alto nivel).

## Impacto en el código

- `core/admin/permissions.py` (`catalog`, `pack_prefixes`, `pack_section_keys`).
- `core/admin/context.py` (catálogo desde los packs activos).
- `core/admin/templates/base.html` (filtro por sub-sección).
- `core/mcp/types.py` (docstring de `admin_menu`).
- `packages/pack-cobranza/pack_cobranza/{module.py,admin.py}` (`section_key`).
