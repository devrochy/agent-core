# ADR-0200 — Crons por negocio: política, ventana y parámetros

- **Estado:** aceptado (plan aprobado por el dueño el 2026-11-16).
- **Fecha:** 2026-11-16
- **Usa:** ADR-0183 (gestión de crons), ADR-0019 (packs por negocio), ADR-0091 (zona horaria del negocio), ADR-0047 (RBAC).
- **Relacionado:** ADR-0201 (fechas y horas).

## Contexto

Los 39 crons del beat son globales: no llevan `tenant_id` y recorren los negocios por dentro
(ADR-0183). Cada cron de pack ya filtra por `enabled_packs` del AgentSpec y lee la configuración
del pack por negocio, pero no existe una política por negocio que responda:

- ¿esta automatización debe actuar sobre **mi** negocio?;
- ¿a qué hora local o dentro de qué ventana?;
- ¿con qué parámetros (horas de reenganche, recordatorios…)?;
- ¿qué hizo por mí la última vez?

El dueño pidió una sección por negocio, con permisos de lectura/escritura para el colaborador.

## Decisión

### 1. Dos capas, no una

- **Operación de plataforma** (ADR-0183, sin cambios): una tarea de beat por cron, pausa global,
  ejecución manual y horario global. Solo master. Los crons de plataforma (salud de canales,
  cobros de la plataforma, retenciones legales, latido, outbox) **no** se configuran por negocio.
- **Política por negocio** (nueva): `tenant_cron_configs` con RLS. Cada cron de negocio consulta
  su fila dentro del `tenant_scope` y decide si actúa, cuándo y con qué parámetros.

**No** se crean entradas de beat por negocio. El beat es una sola instancia y el planificador
reprograma por `task_name`, no por tenant; `tenant × cron` multiplicaría el montículo y los locks.

### 2. Catálogo: ámbito y capacidad

`CronMeta` gana `ambito` (`plataforma` | `negocio`), `configurable`, `hora_local`, `ventana`,
`parametros` y `pack`. Un cron de negocio puede ser visible pero no configurable (tareas internas
de corrección). Un cron de plataforma no admite configuración por negocio: el catálogo lo valida
al importar y el test lo cierra.

### 3. Persistencia: dos tablas de negocio

`tenant_cron_configs` (una fila por negocio×cron configurado):

- `habilitada`, `hora_local`, `ventana` (`{desde, hasta, dias}`), `last_run_at`, `last_status`,
  `last_resumen`, `last_acciones`, `updated_by`, `deleted_at`.
- RLS forzada por `app.tenant_id`; único `(tenant_id, cron_name)`; sin fila = default del catálogo
  (pack habilitado para crons de pack, siempre encendido para los de core).
- `tenant_cron_runs` (historial detallado): solo ejecuciones **con efecto**, fallos y manuales.
  Retención 90 días. Nunca se guarda cada barrido sin efecto: 34 crons × N negocios × 96
  ejecuciones/día sería ruido. La purga va por `admin_purgar_tenant_cron_runs` (SECURITY DEFINER):
  `app_runtime` no puede borrar filas de otro negocio bajo RLS.
- La vista master «Por negocio» lee `admin_tenant_cron_resumen(p_cron)` (SECURITY DEFINER, patrón
  ADR-0039): devuelve solo ids y metadatos operativos (encendido, hora, última ejecución y fallos
  en 24 h), nunca datos de clientes.

### 4. Hora local sin horarios de beat por tenant

Para crons **diarios**, el beat corre **cada hora** y cada negocio comprueba si en su zona
(`app_settings.timezone`) ya es su hora local. El trabajo real solo ocurre a la hora correcta; las
otras 23 pasadas son un chequeo barato. Cada cron trae `hora_local_por_defecto` (la hora que se usa
mientras el negocio no elige), así que un negocio sin configurar no corre 24 veces al día. Para
crons **frecuentes** (cada 5–15 min) no aplica: solo encender/apagar y ventana horaria.

### 5. Parámetros: una sola fuente

Los parámetros de dominio (`follow_up_hours`, `recordatorios_horas`, avisos de seguimiento) siguen
viviendo en la configuración del pack (`plugin_configs`). La pantalla de Automatizaciones los
**enlaza**; no los duplica. Si más adelante se editan en línea, la API hará de proxy al guardado
del pack sin migrar datos.

### 6. Permisos

Nueva sección `automatizaciones`:

- default: dueño `write`, colaborador `read`;
- el dueño otorga `read`, `write` o `none` por colaborador en Colaboradores;
- los crons `sensitive` (dinero o mensajes a clientes) piden confirmación reforzada y se auditan,
  pero un colaborador con `write` puede operarlos (decisión del dueño).

La pausa global del master manda: si un cron está pausado en la plataforma, ningún negocio lo
corre y la pantalla del dueño lo muestra como «pausado por la plataforma».

## Alternativas descartadas

| Opción | Por qué no |
|---|---|
| Tareas de beat por negocio | Una sola instancia de beat, `tenant × cron` entradas, locks y reprogramación por tenant. |
| Guardar la política en `tenants.settings` (JSONB) | Sin restricciones, sin consultas por cron, sin historial y con riesgo de read-modify-write. Se prefiere tabla con RLS. |
| Duplicar los parámetros del pack en la tabla de crons | Dos fuentes de verdad. |
| Exponer todos los crons al dueño | Salud de canales, cobros de la plataforma y retenciones legales son de plataforma. |

## Consecuencias

- Migración `0140` (`tenant_cron_configs`) y `0141` (`tenant_cron_runs`).
- `core/admin/crons_negocio_service.py` es el único escritor de las tablas; un test lo vigila.
- `core/tasks/cron_negocio.py` da `debe_correr`/`registrar` a los ~15 crons de negocio.
- API `core/api/routers/automatizaciones.py` y pantalla `/configuracion/automatizaciones`.
- El master conserva `Plataforma › Crons` y gana la vista por negocio.
- Los crons diarios de negocio pasan a cadencia horaria: el historial global muestra más
  ejecuciones; se etiqueta y el historial por negocio solo registra cuando hubo efecto.
