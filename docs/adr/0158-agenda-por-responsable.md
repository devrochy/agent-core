# ADR-0158 — Agenda por responsable en pack-calendar

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-05. **Enmienda:** ADR-0084 (agenda interna).

## Contexto

La exclusión `ex_pack_calendar_appointments_sin_solape` es `(tenant_id WITH =, tstzrange WITH &&)`: dos citas internas del mismo negocio nunca se solapan, aunque tengan distinto `asignado_admin_id`. Cuatro asesores no pueden mostrar inmuebles a la misma hora. Además, `create_calendar_event` exige el correo en tiempo de ejecución, y la cita no sabe de qué inmueble es.

## Decisión

La agenda interna pasa a ser **por responsable** cuando la cita lo tiene.

1. Columna `agenda_admin_id` (nullable). La exclusión nueva es `(tenant_id WITH =, coalesce(agenda_admin_id, '00000000-…') WITH =, tstzrange WITH &&)` con el mismo `WHERE`.
   - Con `agenda_admin_id` nulo, la conducta es la de hoy: un solo carril por negocio.
2. Columnas `referencia` (texto opaco del pack que crea la cita, por ejemplo `inmobiliario:visita:<id>`) y `origen` (pack que la creó).
3. Correo opcional por configuración: `CalendarConfig.correo_obligatorio` (por defecto `true`, como hoy). Sin correo no se envía el correo determinista de la cita.
4. Migración en una sola transacción con `lock_timeout`: ADD de la exclusión nueva y DROP de la vieja.
   - Va en ventana de mantenimiento, porque toma ACCESS EXCLUSIVE.
   - No hace falta verificar solapes: la clave nueva es más fina que la actual.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Un "calendario" por asesor con su propio `CalendarConfig` | Duplica horarios y bloqueos y complica el panel |
| Validar el solape en código | Pierde la garantía del esquema bajo concurrencia (incidente del deadlock de #326) |

## Consecuencias

**Positivas:** varios responsables por negocio, para visitas, consultorios o técnicos, sin romper los negocios actuales.

**Negativas / lo que aceptamos pagar:** una migración con bloqueo corto sobre una tabla compartida, que pide ventana de mantenimiento.

**Qué haría que revisáramos esto:** necesitar recursos que no son personas (salas, vehículos), que pedirían una entidad `recurso`.

## Impacto en el código

- `packages/pack-calendar/alembic/` (migración nueva).
- `pack_calendar/models.py` y `skills.py`: correo opcional, responsable y referencia.
