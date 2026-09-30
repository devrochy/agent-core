# ADR-0061 — `admin_menu` de packs por secciones y visibilidad por rol

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

El `admin_menu` de un pack es una lista plana de items `{label, path}` y el core
la renderiza como un grupo por plugin (`active_admin_menu`, `base.html`). El
panel de referencia de cobranza (`app-cobranza-admin` develop) agrupa su
navegación en **secciones** ("General", "Monitoreo", "Seguridad") y oculta items
según el rol (Monitoreo/Seguridad solo para el admin de plataforma). El pack no
puede expresar ninguna de las dos cosas: su menú queda plano y sin visibilidad.

## Decisión

Los items de `admin_menu` admiten dos claves opcionales:

1. **`section`** (str): etiqueta de agrupación dentro del grupo del pack
   ("General", "Monitoreo", "Seguridad"). `base.html` agrupa por `section`
   **preservando el orden declarado** (no alfabético) y renderiza la etiqueta.
2. **`master_only`** (bool): el item solo lo ve un admin `master` (mapea el "solo
   admin" del panel de referencia; en agent-core el `master` es el operador de
   plataforma). Sin la clave, el item es visible para quien tenga acceso a la
   sección del pack.

`active_admin_menu` pasa las claves tal cual; el render decide. **No cambia** el
contrato congelado ni el gate de RBAC por sección (el item se sigue filtrando por
`perms`); `master_only` es una capa adicional de visibilidad, no una autorización
(el handler real revalida con `require_section`).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Navegación propia del pack (shell aparte) | Duplica el shell del core y diverge del resto del panel |
| Dejar el menú plano | No respeta la IA del panel de referencia |
| Sub-sección RBAC por item (permiso por item) | Alcance mayor; se deja como follow-up. `section` + `master_only` cubren la IA v1 |

## Consecuencias

**Positivas:** el pack declara su IA de navegación (secciones + visibilidad) sin
tocar el core más allá del render; reutilizable por cualquier pack.

**Negativas / lo que aceptamos pagar:** `master_only` no distingue tenants ni
permisos finos; la granularidad por acción queda para sub-secciones RBAC futuras.

**Qué haría que revisáramos esto:** que un pack necesite ocultar items por
permiso granular (→ sub-sección RBAC) o por tenant (→ condición por spec).

## Impacto en el código

- `core/mcp/types.py` (docstring de `BaseModule.admin_menu`).
- `core/admin/templates/base.html` (agrupar por `section`, filtrar `master_only`).
- `core/plugin_manager.py::active_admin_menu` (pasa las claves sin cambio).
- `packages/pack-cobranza/pack_cobranza/module.py` (`admin_menu` con `section`).
