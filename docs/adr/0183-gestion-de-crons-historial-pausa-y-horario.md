# ADR-0183 — Gestión de crons: catálogo por módulo, historial, pausa, ejecución manual y horario reprogramable

- **Estado:** aceptado (el dueño aprobó el plan el 2026-11-02: solo master, solo crons periódicos).
- **Fecha:** 2026-11-02
- **Contexto:** pedido del dueño: «una sección para los crons del proyecto, clasificados por módulos, con historial de ejecución, estado, ejecución manual, reprogramar y demás acciones para gestionarlos».
- **Usa:** ADR-0044 (registro de crons de packs, `register_cron`), ADR-0107 (outbox de dominio, que corre por el beat), ADR-0047 (RBAC) y el patrón de `platform_readiness_runs` (migración 0064, HU-CONN-07).

## Contexto

El beat de Celery programa 34 tareas periódicas: 21 de core (`core/tasks/celery_app.py`) y 13 de packs (`@register_cron`). Antes de este ADR no había forma de verlas ni operarlas, y tres hechos lo impedían:

1. **El planificador es el `PersistentScheduler` por defecto.** Lee el horario del código al arrancar; pausar o cambiar un horario exigía un despliegue. Su archivo `celerybeat-schedule` vive dentro del contenedor, así que `last_run_at` se pierde en cada redeploy.
2. **Todo wrapper de cron traga la excepción** (`try / run_async / except → log.exception`). Celery ve `SUCCESS` aunque el cron falle, así que ni el result backend ni la señal `task_failure` sirven para un historial: hay que medir dentro del wrapper.
3. **No existía registro de ejecuciones.** Lo más parecido, `platform_readiness_runs`, es de otro asunto.

Los crons son **globales de la plataforma**: no llevan `tenant_id` y recorren los negocios por dentro.

## Decisión

### 1. Catálogo explícito: `core/tasks/cron_catalog.py`

Fuente única de la clasificación por módulo, la etiqueta, la descripción y el **horario por defecto** de cada cron, más sus banderas: `critical` (no se puede pausar: latido del worker y outbox), `sensitive` (toca dinero o escribe a clientes: confirmación reforzada en la pantalla), `reprogramable` (falso en los de intervalo fijo) y `record_success` (falso en los que correrían miles de veces al día: sus fallos y ejecuciones manuales sí se guardan).

La API y el planificador leen de aquí y **no de `celery.conf.beat_schedule`**: ese diccionario solo contiene los crons de los packs una vez cargados los plugins, y la API no siempre los carga. `tests/test_cron_catalogo.py` cierra el catálogo en los dos sentidos: cada entrada del beat debe estar clasificada (un cron nuevo sin metadata rompe el CI) y cada horario por defecto debe coincidir con el crontab del código.

### 2. Un envoltorio común: `core/tasks/cron_runtime.py::run_cron`

Todos los crons pasan por él (`core_cron` en core y `register_cron` en packs). Hace, en orden: pausa (una ejecución *programada* de un cron pausado sale en silencio y sin fila; una *manual* la ignora, es una orden explícita) → motivo de omisión del pack (`plugin_inactivo`) → **lock en Redis** `cron:run:<nombre>` (`SET NX EX 330`; el que llega segundo queda `skipped/en_ejecucion`) → fila `running` → ejecución → cierre `success|failed` con la duración y **solo la clase de la excepción** (el mensaje de un error de SQLAlchemy trae parámetros de consulta, que pueden ser datos de un negocio).

- **Sigue sin relanzar** la excepción (comportamiento de siempre: un fallo no mata al worker ni dispara reintentos).
- **El registro del historial es *best-effort***: si la base o Redis fallan, el cron corre igual y queda un `cron_registro_fallido` en el log. Peor es no cobrar que cobrar sin historial.
- La tarea Celery acepta `trigger` y `triggered_by` como kwargs. El beat no manda ninguno; `POST …/run` manda `trigger="manual"` y el admin. Se usan kwargs y no headers: ningún cron recibe otros argumentos y los headers personalizados han sido inconsistentes entre versiones del protocolo.

### 3. Persistencia: dos tablas de sistema (migración 0120)

`cron_job_configs` (`task_name` PK, `paused`, `schedule_override`, `updated_by`) y `cron_runs` (estado, disparador, quién, duración, clase del error, motivo de omisión). **Tablas de SISTEMA sin RLS**: no guardan datos de ningún negocio, igual que `platform_readiness_runs`. Solo las toca `core/admin/crons_service.py` (`tests/test_crons_tablas_solo_desde_su_servicio.py` lo vigila: ni el modelo ni el servicio se usan desde otro lado). `app_runtime` recibe `SELECT/INSERT/UPDATE` y, solo sobre `cron_runs`, `DELETE`.

Retención: 30 días, con el cron `core.cron_runs_retention` (diario), que además cierra como fallidas las ejecuciones `running` de más de 10 minutos (worker caído). Mientras tanto la API ya las muestra como fallidas con la clase `Interrumpida`.

### 4. Horario reprogramable: `core/tasks/cron_scheduler.py::DbOverrideScheduler`

Reemplaza al `PersistentScheduler` con `-S core.tasks.cron_scheduler:DbOverrideScheduler` (en `scripts/entrypoint-beat.sh` y `docker-compose.yml`). Cada 30 s lee `cron_job_configs` y aplica el horario reprogramado; quitar la reprogramación vuelve al del código. Tres trampas de Celery, resueltas y con test cada una:

- `merge_inplace(b)` **borra** del planificador todo lo que no esté en `b`: siempre se le pasa una copia **completa** de `beat_schedule` con los overrides aplicados.
- `merge_inplace` **muta las entradas en el mismo objeto** que guarda `old_schedulers` (copia superficial), así que `schedules_equal` no ve el cambio y el montículo de próximas ejecuciones queda con los tiempos viejos. Se verificó con Celery 5.6.3. Al cambiar un horario se fuerza `_heap = None`.
- El hook de `beat_init` (`_load_plugins_on_beat_start`) vuelve a hacer `merge_inplace(beat_schedule)` con los crons de los packs, lo que deshace las reprogramaciones: tras la fusión llama a `refrescar_overrides(force=True)`.

Si la base no responde, el beat sigue con lo que tiene (nunca se queda sin crons) y registra `cron_scheduler_lectura_fallida`.

La expresión es de 5 campos en UTC (`parse_expression`: solo cifras, `*`, `/`, `,` y `-`). Poner la expresión por defecto equivale a quitar la reprogramación.

### 5. API y permisos: `core/api/routers/crons.py`

`/api/v1/admin/platform/crons` (listado con filtros por módulo/estado/texto, resumen, detalle, historial paginado, `run`, `pause`, `resume`, `PUT/DELETE schedule`). **Solo `es_master`**, también en test: un tester **no** opera crons (cobrar, avisar a clientes), a diferencia de otras herramientas de plataforma que usan `actua_como_master`. Cada vista devuelve las `acciones` válidas para su estado, y el panel solo ofrece esas (sin 403 por diseño).

## Alternativas descartadas

- **`django-celery-beat` / `redbeat`:** dependencia nueva (y, en el primero, Django) para resolver una lectura de dos columnas; ya existe el punto de integración (`merge_inplace`) y el beat ya tiene `DATABASE_URL`.
- **Historial desde el result backend o `task_failure`:** Celery nunca ve el fallo porque el wrapper lo traga; además los resultados caducan al día y comparten la DB 0 de Redis con la memoria de trabajo.
- **Tabla por negocio con RLS:** los crons son de la plataforma; no hay `tenant_id` que poner y obligaría a `admin_enum` para leerla.
- **Sacar la pausa del planificador (quitar la entrada del beat):** funcionaría, pero el chequeo en el wrapper cubre también las ejecuciones ya encoladas y la manual necesita decidir si respeta la pausa. Una tarea omitida cuesta un mensaje en Redis.

## Consecuencias

- **Despliegue:** el cambio de planificador exige reiniciar el beat (el entrypoint cambia) y aplicar la migración 0120 antes que el worker nuevo; un worker nuevo sin la 0120 deja `cron_registro_fallido` en el log pero los crons siguen corriendo. El beat debe tener `DATABASE_URL` (ya lo exige el runbook).
- Un cron nuevo debe agregarse al catálogo en el mismo PR (el test lo exige).
- Reprogramar `core.billing_renovacion` o `core.billing_lifecycle` mal puede cobrar de más o no cobrar: por eso solo master, confirmación reforzada (`sensitive`) y botón para restaurar el horario del código.
- El latido del worker (`core.worker_heartbeat`) y el despachador del outbox (`core.domain_events_dispatch`) no guardan sus éxitos: la pantalla lo dice («no se registran ejecuciones exitosas») en vez de mostrar «nunca».
- Una ejecución manual no se solapa con otra en curso (409), pero ignora la pausa.
- Sigue habiendo **una sola instancia de beat** (`docs/despliegue-coolify.md`): el planificador nuevo no cambia eso.
