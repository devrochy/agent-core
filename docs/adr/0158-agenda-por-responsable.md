# ADR-0158 — Agenda por responsable en pack-calendar

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-05. **Enmienda:** ADR-0084 (agenda interna).

## Contexto

La exclusión `ex_pack_calendar_appointments_sin_solape` es `(tenant_id WITH =, tstzrange WITH &&)`: dos citas internas del mismo negocio nunca se solapan, aunque tengan distinto `asignado_admin_id`. Cuatro asesores no pueden mostrar inmuebles a la misma hora. Además, `create_calendar_event` exige el correo en tiempo de ejecución, y la cita no sabe de qué inmueble es.

## Decisión

La agenda interna pasa a ser **por responsable** cuando la cita lo tiene.

1. Columna `agenda_admin_id` (nullable), solo para el **modo interno** (la exclusión no aplica a citas de Google). La exclusión nueva es:

   `EXCLUDE USING gist (tenant_id WITH =, (coalesce(agenda_admin_id, '00000000-0000-0000-0000-000000000000'::uuid)) WITH =, tstzrange(start_at, end_at) WITH &&) WHERE (status = 'scheduled' AND deleted_at IS NULL AND google_event_id IS NULL)`

   - Con `agenda_admin_id` nulo, la conducta es la de hoy: un solo carril por negocio.
   - Una cita con responsable y una sin responsable no chocan en la base. Por eso la validación en código (punto 5) trata las citas sin responsable como ocupación de todos los carriles.
   - La migración repite `CREATE EXTENSION IF NOT EXISTS btree_gist`.
2. Columna `referencia` (texto opaco `<pack>:<ref>`, por ejemplo `pack_inmobiliario:visita:<id>`, que compone el core en ADR-0159). El pack de origen se lee de su prefijo: no hay columna `origen` aparte.
3. Correo opcional **por tipo de cita**: `appointment_types.correo_obligatorio` (por defecto `true`, como hoy). Sin correo no se envía el correo determinista de la cita.
4. **Bloqueos por responsable:** `CalendarBlock.admin_id` (nullable), validado contra `tenant_user`. Un bloqueo con responsable solo ocupa su carril; sin responsable, el de todo el negocio (vacaciones de un asesor no bloquean a los demás).
5. **Disponibilidad y validación por carril:** `_local_busy`, `_validate_internal_slot` y el mover cita del panel (`admin_api.py`) filtran por carril. Con `agenda_admin_id` cuentan solo las citas de ese responsable, las citas sin responsable y los bloqueos del negocio o de ese responsable. Sin él, todo el negocio, como hoy.
6. Migración en una sola transacción con `lock_timeout`: ADD de la exclusión nueva y DROP de la vieja.
   - Va en ventana de mantenimiento, porque toma ACCESS EXCLUSIVE.
   - No hace falta verificar solapes: la clave nueva es más fina que la actual.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Un "calendario" por asesor con su propio `CalendarConfig` | Duplica horarios y bloqueos y complica el panel |
| Validar el solape en código | Pierde la garantía del esquema bajo concurrencia (incidente del deadlock de #326) |
| Reutilizar `asignado_admin_id` en la exclusión | Los negocios que ya tienen tipos con responsables distintos pasarían a admitir solapes sin pedirlo; `agenda_admin_id` solo lo pone quien agenda por responsable |

## Consecuencias

**Positivas:** varios responsables por negocio, para visitas, consultorios o técnicos, sin romper los negocios actuales.

**Negativas / lo que aceptamos pagar:** una migración con bloqueo corto sobre una tabla compartida, que pide ventana de mantenimiento.

**Qué haría que revisáramos esto:** necesitar recursos que no son personas (salas, vehículos), que pedirían una entidad `recurso`.

## Impacto en el código

- `packages/pack-calendar/alembic/` (migración nueva: exclusión, `agenda_admin_id`, `referencia`, `appointment_types.correo_obligatorio`, `CalendarBlock.admin_id`).
- `pack_calendar/models.py`.
- `pack_calendar/skills.py` (`_local_busy`, `_validate_internal_slot`, correo opcional).
- `pack_calendar/admin_api.py` (mover cita, bloqueos por responsable).
- `pack_calendar/demo.py` (payload de `appointment.changed`, ver ADR-0159).
