# ADR-0044 — Registro de crons de packs (import de `jobs` + `beat_init`)

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

Los packs registran sus tareas programadas con el decorador `@register_cron`, que corre
**al importar** el módulo de jobs del pack. `pack-commerce` hacía
`import pack_commerce.jobs` **dentro de `on_activate()`**
(`packages/pack-commerce/pack_commerce/module.py`).

`on_activate()` solo se ejecuta en el proceso **API** (`core/main.py` llama
`activate_all_configured()`); ni el worker ni el beat lo hacían. Consecuencia real:

- `celery-beat` conocía **solo los crons de core**: en su `beat_schedule` no estaban
  `pack_commerce.follow_up_leads` ni `follow_up_quotes` → **nunca se programaban**.
- El worker tampoco tenía registradas esas tasks → no habría podido ejecutarlas.
- Mismo patrón de bug que `core.outbound_send` (ADR-0042), del lado de los packs.

## Decisión

1. **Importar los jobs a nivel de módulo del pack** (`import pack_commerce.jobs` al tope de
   `module.py`), no dentro de `on_activate()`. Cualquier proceso que **escanee** el plugin
   (API, worker, beat) registra los `@register_cron`.
2. **El beat escanea plugins** vía un handler `@beat_init.connect` en `celery_app.py`
   (que reusa `_load_plugins()`, el mismo que `worker_init`: `plugin_manager.scan()` +
   import de `core.tasks.decorators`).
3. La **ejecución** sigue condicionada por el guard del decorador
   (`plugin_manager.is_active(plugin_id)`): registrar la tarea es barato e inofensivo; que
   corra depende de que el plugin esté activo para el tenant.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Llamar `activate_all_configured()` en el arranque del worker/beat | Es async y hace acceso a BD/config al arrancar; innecesario para solo registrar tareas. |
| Convención `<pack>.jobs` importado por el `plugin_manager` | Frágil (convención no declarada) y agrega acoplamiento core→pack. |
| Registrar los crons solo en el API | No sirve: las tasks corren en el worker y el schedule lo dispara el beat. |

## Consecuencias

- Verificado en vivo: el beat loguea `cron_registered pack=pack_commerce task=…` y el
  worker lista `pack_commerce.follow_up_leads`/`follow_up_quotes` en `inspect registered`.
- Regla para packs nuevos: **el import de su módulo de jobs debe ser a nivel de módulo**
  (documentar en `docs/PLUGIN-CONTRACT.md` si aplica).
- Tests: `tests/test_async_bridge.py::test_beat_scan_registra_crons_de_packs`.
