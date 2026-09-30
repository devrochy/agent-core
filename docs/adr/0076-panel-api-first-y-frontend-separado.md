# ADR-0076 — Panel API-first y frontend separado

- **Estado:** aceptado
- **Fecha:** 2026-09-28

## Contexto

La serie legacy (`docs/ADR/ADR-007`) fijó el panel como **100% SSR** (Jinja2 +
Bootstrap 5), sin API JSON paralela, para no mantener dos superficies. Hoy el
producto tiene **panel web + APK**: la APK (`solvershub-apk`) ya es un consumidor
real de agent-core (rutas de pack), y el panel admin es la única superficie que
**no** expone API. El `FormGenerator` genera la UI de configuración de cada plugin
desde el `config_schema` Pydantic, atando la UI a HTML renderizado en servidor.

## Decisión

El panel deja de ser SSR. En su lugar:

1. agent-core expone una **API JSON versionada `/api/v1`** (superficie y auth en
   ADR-0077). El runtime multi-tenant y RLS no cambian.
2. El panel web vive en un proyecto **separado** (`panel-web/`: Next.js 16 +
   Tailwind v4 + shadcn/ui) y consume un **cliente TypeScript generado** desde el
   OpenAPI de la API. La landing ya usa este stack y sus tokens de marca.
3. La autenticación usa un **token opaco de `AdminSession`** (cookie `httpOnly`
   para la web, `Authorization: Bearer` para apps). Se descarta JWT para el realm
   admin: la sesión en BD permite **revocación inmediata** al desactivar el admin.
4. El `FormGenerator` se sustituye por un **renderer de JSON Schema** en el panel:
   el `config_schema` Pydantic se publica como JSON Schema en la API y el front lo
   renderiza. Un autor de plugin sigue sin escribir UI.

El retiro de las plantillas Jinja es **progresivo**, por sección, sin big-bang.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener SSR y solo re-tematizar | No da API para la APK ni separación real; el panel seguiría acoplado al render |
| HTMX sobre el SSR actual | Añade interactividad pero sigue sin contrato consumible por la app |
| JWT en el realm admin | Pierde la revocación inmediata (lección ADR-025 de agent-sale) |
| GraphQL en vez de REST | El equipo y los SDK ya trabajan REST/OpenAPI; GraphQL añade complejidad sin consumidor que lo pida |

## Consecuencias

**Positivas:** una sola API sirve panel y app; el diseño web se desacopla del
render Python; los tipos TS se generan del contrato real (elimina el drift);
el panel puede reutilizar el sistema de diseño de la landing.

**Negativas / lo que aceptamos pagar:** se reimplementan 108 rutas como endpoints
JSON; se reemplaza el `FormGenerator` (el punto más caro); dos apps web más que
operar; hay que mantener el cliente TS regenerado en cada cambio de API.

**Qué haría que revisáramos esto:** que la API `/api/v1` no logre un spec único
usable por web y app (→ volver a un solo cliente SSR), o que el renderer de JSON
Schema no cubra los `config_schema` reales de los packs.

## Impacto en el código

- `core/api/` (nuevo), `core/admin/auth.py` (resolución Bearer), `core/main.py`
  (montaje de `/api/v1`), `core/admin/form_generator.py` (publicar JSON Schema).
- `panel-web/` (nuevo), cliente TS generado desde OpenAPI.
- Retiro progresivo de `core/admin/templates/*.html`.
- Reemplaza la decisión operativa de `docs/ADR/ADR-007` (legacy).
