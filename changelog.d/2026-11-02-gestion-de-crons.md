## Gestión de crons de la plataforma (ADR-0183, 2026-11-02)

- **API master.** `/api/v1/admin/platform/crons`: catálogo de los 34 crons del beat clasificados por módulo (estado, horario
  vigente y por defecto, próxima ejecución, última ejecución y fallos de 24 h), resumen por módulo, detalle, historial
  paginado, **ejecutar ahora**, **pausar/reanudar** y **reprogramar** (`PUT`) / restaurar (`DELETE`) el horario. Solo el
  master, también en test: el tester no opera crons.
- **Historial.** Cada cron (core y packs) pasa por `run_cron`: registra en `cron_runs` quién lo disparó, cuánto tardó y cómo
  terminó (de un fallo, solo la clase de la excepción), con un lock en Redis que impide que dos ejecuciones del mismo cron se
  solapen. Sigue sin relanzar la excepción. El latido del worker y el outbox no guardan sus éxitos. Retención de 30 días
  (`core.cron_runs_retention`, nuevo cron diario).
- **Horario reprogramable.** Nuevo planificador `DbOverrideScheduler` (`-S core.tasks.cron_scheduler:DbOverrideScheduler`
  en `entrypoint-beat.sh` y `docker-compose.yml`): el beat relee `cron_job_configs` cada 30 s, sin reiniciar. **Requiere
  reiniciar el beat al desplegar.**
- **Migración 0120.** Tablas de sistema `cron_job_configs` y `cron_runs` (sin RLS, sin datos de negocio).
- **Catálogo.** `core/tasks/cron_catalog.py`; un cron nuevo debe agregarse ahí (un test lo exige).
- **Compatibilidad.** Aditivo: los crons siguen con los mismos nombres y horarios (un test lo comprueba); la tarea de cada
  cron acepta `trigger`/`triggered_by` opcionales. `docs/openapi.json` actualizado.
