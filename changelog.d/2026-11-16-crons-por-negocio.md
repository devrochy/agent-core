## Crons por negocio y fechas en la zona del negocio (2026-11-16)

- ADR-0200. Los crons de negocio se pueden encender/apagar, programar a una hora local o dentro
  de una ventana y dejar su último resumen por negocio (`tenant_cron_configs` con RLS). El master
  conserva la operación global (`Plataforma › Crons`); el dueño y el colaborador con `write` usan
  `Configuración › Automatizaciones`. Historial por negocio (`tenant_cron_runs`, solo ejecuciones
  con efecto/fallo/manual, 90 días) y vista master «Por negocio». Los crons diarios pasan a cadencia horaria con
  `hora_local_por_defecto`, para que cada negocio elija su hora sin crear tareas de beat por tenant.
- ADR-0201. Las fechas de los registros se pintan en la zona de `app_settings.timezone`:
  `lib/fechas.ts` + `useFormato()` en el panel, `MeResponse.timezone/currency`, y `formatDateTime`
  deja de usar la zona del servidor (UTC). Los crons de cobranza calculan «hoy» con el reloj del
  negocio (`core/shared/tiempo_negocio.py`) y la API de cobranza devuelve instantes ISO 8601 en vez
  de texto preformateado en UTC.
