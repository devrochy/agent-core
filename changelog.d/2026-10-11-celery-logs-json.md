## Worker y beat de Celery: logs en JSON y tracebacks sin variables locales (2026-10-11)

- El worker y el beat no llamaban a `configure_logging`: structlog quedaba con su default y los
  errores de las tareas (p. ej. `cron_task_failed` de `pack_commerce.follow_up_quotes` en test) salían
  con el traceback de rich y las variables locales de cada frame (`│ nombre = valor`), donde podía
  quedar un token o datos de un cliente. Ahora `celery_app` atiende la señal `setup_logging` (worker,
  beat y cada proceso hijo del worker) y también configura en `worker_init`, que llega antes.
- Con eso Celery ya no reemplaza el handler del logger raíz ni redirige stdout a `celery.redirected`:
  cada línea es un objeto JSON, con el traceback en texto plano en `exception`. Los registros de
  `logging` (los de Celery, como `Task … raised unexpected`, httpx, SQLAlchemy) también salen en JSON,
  en todos los procesos que llaman a `configure_logging` (API y connector incluidos).
- Red de seguridad: un proceso que importa `core.shared.logger` sin configurar nada usa la consola
  sin variables locales en vez del default de structlog.
- El connector ya llamaba a `configure_logging` en su `main()`; no cambia salvo por los logs de
  `logging`, que ahora también van en JSON.
