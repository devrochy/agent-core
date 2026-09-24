# ADR-0057 — Rutas públicas de pack e identidad de pack (auth de usuarios finales)

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

El core autentica solo `Admin`/`TenantUser` (panel) y expone como público únicamente
webhooks firmados y login. Un pack con **usuarios finales propios** (gestores/cobradores
en campo) no encaja: no son admins del panel. El middleware del panel exige sesión y
aplica gate de sección; sin cookie deja pasar la petición, así que hoy la única
superficie pública real es `/webhooks/*`. Hace falta una superficie pública de pack con
identidad propia, aislada del panel.

## Decisión

1. **Convención de rutas públicas de pack:** `/api/pack/<plugin_id>/…`, declaradas por
   el pack vía `BaseModule.public_routes()` (nuevo) y añadidas a la allowlist del
   middleware en `core/main.py`.
2. El pack **emite y valida su propia identidad (JWT)** para sus usuarios finales,
   scoped a `tenant_id`; resuelve el tenant y escribe dentro de `tenant_scope(tenant_id)`.
3. Sin cookie de admin ni gate de sección: **el pack autentica cada request**. Sus
   secretos/llaves viven en su config cifrada (Fernet).
4. Rate limit y auditoría son responsabilidad del pack (con helpers del core).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reusar `/webhooks/*` | Mezcla semántica; no distingue identidad de pack |
| Extender el core con una tabla de usuarios de pack | Toca el modelo de auth del core; más invasivo que dar el punto de extensión |
| Que el pack monte rutas sin declararlas | El middleware las bloquea con sesión; sin sesión pasan sin gate → inseguro por omisión |

## Consecuencias

**Positivas:** el core no conoce los usuarios del pack; superficie pública explícita y
auditable; sirve a cualquier pack con usuarios finales.

**Negativas / lo que aceptamos pagar:** nueva superficie de ataque → exige rate limit,
validación estricta y documentación en `security.md`; el pack asume su propia auth.

**Qué haría que revisáramos esto:** que dos packs necesiten la misma identidad de usuario
final (→ extraer a capacidad de core) o que aparezca una fuga cross-tenant en una ruta
pública (→ apretar a validación central).

## Impacto en el código

- `core/main.py` (allowlist + montaje de `public_routes`).
- `core/mcp/types.py` (`BaseModule.public_routes`).
- `docs/security.md` (documentar la superficie expuesta).
- `packages/pack-cobranza/pack_cobranza/{api.py,auth.py}` (implementación v2).
