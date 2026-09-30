# ADR-0077 — API unificada `/api/v1` y realms de autenticación

- **Estado:** aceptado
- **Fecha:** 2026-09-28

## Contexto

Hoy conviven superficies dispares: el panel SSR no tiene API; los packs exponen
rutas públicas propias `/api/pack/<pack_id>/…` con **JWT de usuario final** scoped
a tenant (ADR-0057); y la API de campo del gestor **replica el contrato de la APK**
(ADR-0065). Con el panel API-first (ADR-0076) y la APK se quiere **una sola API y
un solo contrato**, sin romper a la APK en producción.

## Decisión

Una sola superficie versionada **`/api/v1`** con **un único documento OpenAPI**:

1. **Dos realms de autenticación** que resuelven a un `Principal` común
   (`tenant_id`, identidad, permisos/scope):
   - **admin** → token opaco de `AdminSession` (cookie o `Bearer`).
   - **gestor** → JWT del pack (identidad de usuario final, ya existente).
2. **Namespacing por realm:** `/api/v1/admin/*` y `/api/v1/gestor/*`. Ambos usan
   el **mismo formato de error, paginación y resolución de tenant**.
3. El **tenant activo** viaja en `X-Tenant-Id`, validado server-side contra la
   identidad del `Principal` (nunca desde el body). RLS sigue vigente por request.
4. `/api/pack/*` se conserva como **alias deprecado** hasta que la APK migre a
   `/api/v1/gestor/*`; después se retira.
5. El cliente y los tipos TypeScript se **generan del OpenAPI**, no se escriben a mano.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dos APIs separadas (una admin, una pack) | Doble spec, doble cliente y drift de contratos |
| Convertir `/api/pack/*` en la API definitiva | No cubre el panel admin (identidad y RBAC propios) |
| Un solo realm para todo | Mezcla identidades de admin y de usuario final; rompe el aislamiento de ADR-0057 |
| GraphQL | Sin consumidor que lo pida; REST/OpenAPI ya encaja con el SDK generado |

## Consecuencias

**Positivas:** un contrato para panel y app; tipos TS siempre en sincronía;
la APK migra sin corte; el aislamiento multi-tenant se mantiene por request.

**Negativas / lo que aceptamos pagar:** un middleware/tabla de enrutado de realms
más; durante la transición conviven `/api/v1/*` y `/api/pack/*` (doble superficie
temporal); el pack sigue cargando el contrato "ajeno" camelCase de la APK (ADR-0065).

**Qué haría que revisáramos esto:** que la APK no pueda migrar a `/api/v1/gestor/*`
sin romper su contrato, o que aparezca una fuga cross-tenant por una ruta `/api/v1`.

## Impacto en el código

- `core/api/` (nuevo: `deps.py`, `errors.py`, `pagination.py`, `routers/`).
- `core/main.py` (montaje `/api/v1` + allowlist del middleware), `core/admin/auth.py`.
- `packages/pack-*/…/public_routes` y `solvershub-apk/src/{config.ts, api/*}` en la
  fase de migración de la APK.
- `docs/security.md` / `solvershub/security.md` (nueva superficie pública).
- Modifica ADR-0057 y ADR-0065 (no los reemplaza: sus contratos siguen válidos).
