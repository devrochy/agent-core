# ADR-0042 — Worker Celery: `include` de tasks + puente async con loop persistente

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

Dos bugs independientes del worker de Celery dejaban las respuestas sin salir:

1. **`core.outbound_send` no estaba registrado.** El task se define con
   `name="core.outbound_send"` en `core/tasks/outbound_send.py`, pero el worker **nunca
   importaba ese módulo** al arrancar (`celery_app.py` solo declara tasks inline). El
   broker lo rechazaba con `Received unregistered task` y las filas de `Message` quedaban
   `queued` para siempre.

2. **El puente sync→async reusaba recursos de un loop cerrado.** Los tasks usaban
   `asyncio.run()` por invocación (loop nuevo cada vez), pero `get_engine()` (pool de
   SQLAlchemy) y `get_redis()` son singletons de módulo atados al **primer** loop. El
   segundo task en el mismo proceso fallaba con `RuntimeError: Event loop is closed`
   (`core.worker_heartbeat` fallaba cada minuto; 236 ocurrencias).

## Decisión

1. **Registrar las tasks de core explícitamente** vía
   `Celery(..., include=["core.tasks.outbound_send"])`. `include` obliga al worker a
   importar el módulo (y por lo tanto registrar el `@celery.task`) al finalizar el arranque.
2. **Un único event loop persistente por proceso worker** en
   `core/shared/async_bridge.py::run_async(coro)` (thread daemon + `asyncio.run_coroutine_threadsafe`),
   y reemplazar los `asyncio.run(...)` de `core/tasks/*`, `core/shared/events.py::emit_nowait`
   y `core/db/session.py::run_async` por `run_async(...)`.
   - `run_async` falla claro si se lo llama desde un loop ya corriendo (usar `await`).
   - `_ensure_loop` recrea el loop si el thread del bridge murió (evita un cuelgue
     permanente hasta el `task_time_limit` de Celery).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `import core.tasks.outbound_send` suelto en `worker_init` | Funciona, pero `include` es el mecanismo canónico y también cubre al beat. |
| Hacer `get_engine()`/`get_redis()` "loop-aware" (cache por loop) | Más superficie y manejo de limpieza; el loop persistente resuelve **todos** los singletons de una vez. |
| `NullPool` + cliente Redis por loop | Más piezas para el mismo objetivo; no arregla el patrón general. |

## Consecuencias

- El worker puede ejecutar `core.outbound_send` (y los tasks de los packs, ver ADR-0044).
- Los singletons async viven en un loop vivo durante toda la vida del proceso.
- El API (uvicorn) ya tiene su loop y no usa `run_async`.
- Tests: `tests/test_async_bridge.py` (registro de `core.outbound_send`; reutilización del
  loop; guard; recreación si el thread murió).
