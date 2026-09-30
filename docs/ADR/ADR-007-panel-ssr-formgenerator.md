# ADR-007 · Panel SSR (Jinja2 + Bootstrap 5) + FormGenerator desde Pydantic

**Estado**: Reemplazada por ADR-0076 · **Fecha**: 2026-08-19

## Contexto

agent-dashboard usa React SPA (build step, API JSON paralela, 30+ archivos de UI).
agent-sale usa SSR puro en TypeScript y evolucionó más rápido con menos código.
Los plugins necesitan UI de configuración sin que su autor escriba HTML.

## Decisión

1. Panel 100% SSR: FastAPI + Jinja2 + Bootstrap 5 por CDN. Cero build step, cero
   API JSON paralela, cero estado en el cliente.
2. **FormGenerator**: el `config_schema` (Pydantic) de cada plugin se convierte
   automáticamente en formulario Bootstrap: SecretStr→password (cifrado Fernet al
   guardar, vacío conserva el valor), bool→checkbox, Literal→select, int/float→number,
   `Field(description=)`→texto de ayuda.
3. Menú lateral dinámico: `plugin_manager.active_admin_menu()` — al desactivar un
   plugin su UI desaparece en el siguiente request.
4. Los plugins pueden aportar un `admin_router` (APIRouter) propio para vistas
   avanzadas (ej. el flujo OAuth de pack-calendar).
5. Auth: sesión en BD con cookie firmada + bcrypt. No JWT (sin revocación inmediata
   — lección del ADR-025 de agent-sale).

## Consecuencias

- (+) Un plugin obtiene UI de configuración con CERO líneas de HTML.
- (+) Deploy = un proceso; sin sincronizar versiones front/back.
- (−) Interactividad limitada a forms/redirects (suficiente para un panel de
  operación; si algún plugin necesita más, trae su propio router con HTMX).
