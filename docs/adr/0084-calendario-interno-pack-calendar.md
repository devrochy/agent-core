# ADR-0084 — Calendario interno del pack-calendar (sin Google)

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Modifica:** ADR-0072 (gating declarativo de tools por conexión)
- **Relacionado:** ADR-0058 (PostGIS), ADR-0069 (tipos de conexión)

## Contexto

Las cinco tools de `pack-calendar` (`check_availability`, `create_calendar_event`,
`update_calendar_event`, `list_client_appointments`, `cancel_calendar_event`) declaran
`requires_connection_types=["google_calendar"]` (`packages/pack-calendar/pack_calendar/skills.py:100,116,157,178,212`).
Sin una conexión Google activa, el gating del loop (ADR-0072) las **quita del prompt** y
las deniega, así que el asistente **no puede agendar nada** (`core/orchestrator/loop.py:245-266,731-742`).

Además, aun con Google:
- `create_calendar_event` no acepta `duration_minutes` ni `appointment_type_id` (usa
  `CalendarConfig.event_duration_minutes`, `skills.py:217-265`).
- `check_availability` devuelve **ocupados + ventana de atención**, pero **no calcula slots
  libres** (`skills.py:599-634`): el LLM tiene que razonarlos → riesgo de alucinación.
- El `flow.json` default solo expone `check_availability`, `create_calendar_event`,
  `send_email` (sin reagendar/cancelar/listar).
- No hay tool para `AppointmentType` (solo existe el CRUD del panel, `admin_api.py:76-129`).

Para el asistente de la agencia, el **agendamiento de demos no puede depender de Google
Calendar**: bloquea el flujo completo hasta tener credenciales OAuth.

## Decisión

Añadir al `pack-calendar` un **modo de calendario interno** (agenda propia, en BD), sin
depender de Google, con las mismas tools:

1. **Fuente configurable** `CalendarConfig.source` ∈ `interno | google` (default `interno`).
2. **Disponibilidad interna**: `BusinessHours` (franjas por día) y `CalendarBlock`
   (bloqueos puntuales/vacaciones) — más `AppointmentType`/`Appointment`/`AppointmentEvent`
   que ya existen (`pack_calendar/models.py:35-119`).
3. **Gating condicional**: las tools dejan de requerir `google_calendar` cuando
   `source=interno`; en modo `google` mantienen `requires_connection_types` (ADR-0072). El
   requisito se resuelve en tiempo de arranque del pack (override del atributo por config).
4. **`check_availability` calcula slots libres reales**: `business_hours` − citas −
   `CalendarBlock` − `buffer_minutes`, con `slot_minutes` y `duration_minutes` del tipo de
   cita; devuelve `free_slots[]` (además de `busy_slots[]`).
5. **`create_calendar_event` acepta `duration_minutes`** (default del tipo o config) y
   `appointment_type_id`.
6. **Flow default ampliado**: `check_availability`, `create_calendar_event`,
   `update_calendar_event`, `cancel_calendar_event`, `list_client_appointments`, `send_email`.
7. **Tool de tipos**: `list_appointment_types` (para que el asistente elija “Demo”, etc.).
8. En modo `google`, el proveedor sigue siendo Google (`oauth.py`); en `interno`, sin red.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Exigir Google Calendar siempre | Bloquea demos/agenda sin credenciales; el tenant `solvershub` no las tiene |
| Pack nuevo `pack_agenda` | Duplica `Appointment`/`AppointmentType` y el OAuth de Google |
| Calcular slots en el prompt (LLM) | Fuente de alucinación (ADR-0025 lo viene a evitar) |
| Dejar `check_availability` como está | El LLM inventa horarios libres |

## Consecuencias

**Positivas:** agenda funcional sin credenciales (demos ya); disponibilidad determinista
(sin alucinar); Google queda como modo opcional; reusa los modelos existentes.

**Negativas / lo que aceptamos pagar:** dos fuentes de verdad (agenda interna vs Google)
que hay que mantener coherentes si luego se conecta Google; `BusinessHours`/`CalendarBlock`
nuevos; cambiar el gating de un pack activo exige cuidado (tests).

**Qué haría que revisáramos esto:** necesitar sincronización bidireccional real con Google,
o agenda compartida entre varios tenants (pooled).

## Impacto en el código

- `packages/pack-calendar/pack_calendar/models.py` (`BusinessHours`, `CalendarBlock`) + migración.
- `config.py` (`source`, slots/buffer), `skills.py` (slots libres, `duration_minutes`,
  `appointment_type_id`, `list_appointment_types`, gating condicional).
- `flow.json` default + `playbooks/citas.md` + `module.py` (claims).
- `admin_api.py` (tipos/bloqueos), tests unit + integración, evals de agendamiento.
