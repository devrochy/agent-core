# ADR-0079 — API admin de packs bajo `/api/v1/pack/<id>` (punto de extensión)

- **Estado:** aceptado
- **Fecha:** 2026-09-28

## Contexto

El panel nuevo (`solvershub/apps/panel-web`, ADR-0076) consume la API unificada
`/api/v1` (ADR-0077). Los packs (Cobranza, Comercio, Calendario) siguen sirviendo su
UI como **SSR** en el panel clásico (`admin_router` propio) y hoy se consumen
enlazados vía `/legacy`. Reconstruirlos en Next necesita que **el pack exponga su
propia API**, sin que el core importe packs (invariante 5 y ADR-0056) ni se duplique
su lógica de dominio.

Los puntos de extensión existentes son `admin_router`/`extra_admin_routers` (HTML bajo
`/admin`) y `public_routes` (API pública con identidad propia del pack, ADR-0057).
Ninguno sirve para una **API admin** consumida por el panel con la identidad del admin.

## Decisión

Se añade el punto de extensión **`BaseModule.admin_api_routes()`**: el pack declara
`APIRouter`s que el core monta en la API unificada.

1. **Prefijo obligatorio** `/api/v1/pack/<plugin_id>` (el core lo valida y avisa si no
   lo cumple, igual que `public_routes`).
2. **Autenticación: realm admin** — las rutas usan las dependencias del core
   (`core.api.deps.require("<section_key>")`), así que heredan cookie/Bearer,
   `X-Tenant-Id` validado y RBAC por (sub)sección (ADR-0047/0063). El pack **no**
   implementa auth propia aquí (a diferencia de `public_routes`).
3. **El core no conoce el dominio**: solo monta routers; el pack resuelve sus tablas
   (`pack_*/…`) dentro del `tenant_scope` del request.
4. Los routers de packs se incluyen en el **OpenAPI exportado** (`scripts/export_openapi.py`),
   así que el panel genera tipos TS para ellos como para el resto.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener packs solo por `/legacy` | Es la situación actual: funciona, pero no es el panel nuevo ni tiene tipos |
| Que el core implemente el dominio de cada pack | Rompe invariante 5 y obliga a tocar el core por cada vertical |
| `public_routes` con identidad del pack para el panel | El panel es identidad **admin**, no usuario final; mezclaría realms |
| Un servicio aparte por pack | Más despliegues y otra auth; sobredimensionado |

## Consecuencias

**Positivas:** los packs reconstruyen su UI en el panel nuevo sin tocar el core más
allá del montaje; el contrato TS se genera solo; el RBAC y el aislamiento por tenant
son los mismos del resto de la API.

**Negativas / lo que aceptamos pagar:** un punto de extensión más en el contrato de
plugins; conviven dos UIs del pack (nueva y `/legacy`) durante la transición; el pack
asume mantener la paridad funcional de su API con su UI SSR.

**Qué haría que revisáramos esto:** que el 90 % de los packs necesite el mismo
conjunto de endpoints genéricos (→ extraer una capacidad de core, como se hizo con
`register_view`), o que un pack necesite identidad distinta de la del admin.

## Impacto en el código

- `core/mcp/types.py` (`admin_api_routes`), `core/main.py` (montaje + aviso de prefijo),
  `scripts/export_openapi.py` (incluir routers de packs).
- `packages/pack-*/…/api.py` (nuevo en cada pack que se reconstruya).
- `apps/panel-web/app/api/pack/[...path]/route.ts` (proxy BFF genérico para acciones).
