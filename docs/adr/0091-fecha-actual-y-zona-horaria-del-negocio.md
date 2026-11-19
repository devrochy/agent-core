# ADR-0091 — Fecha actual en el prompt y zona horaria del negocio

- **Estado:** aceptado
- **Fecha:** 2026-10-04
- **Relacionado:** ADR-0084 (calendario interno), ADR-0026 (override de playbooks por tenant), ADR-0050 (persona del spec)
- **Contexto:** HU-CAL-04 (SPEC-24, `solvershub-docs`), decisiones D-SPEC-24-5 y D-SPEC-24-6

## Contexto

Ningún bloque del system prompt trae la fecha actual (`core/llm/prompt_builder.py:78-137`): en la
prueba con clientes reales el asistente agendó "el próximo miércoles" dos semanas tarde y consultó
fechas pasadas. El core tampoco tiene una zona horaria del negocio: solo existe la del horario de
atención (`escalation_config.business_hours.tz`, que se descarta si no hay rangos,
`core/api/routers/settings.py:321-324`) y la del pack de calendario (`CalendarConfig.timezone`).

## Decisión

El negocio tiene una zona horaria propia en `app_settings.timezone` y el prompt de cada turno trae la
fecha y hora actual de esa zona como **dato** al final del bloque dinámico; la **instrucción** de
cómo usarla vive en playbooks versionados.

- Migración core `0050`: `app_settings.timezone text not null default 'America/Bogota'`.
- Se edita en Configuración → Identidad (validada con `ZoneInfo`; inválida → 422; omitida en el
  PUT → no cambia). El horario de atención la usa cuando `business_tz` viene vacío.
- `PromptContext.timezone`; `build_system_blocks` agrega como **última** parte de `dynamic_parts`
  `## Fecha y hora actual` + "Hoy es <día>, <d> de <mes> de <año> — <HH:MM> (<zona>)." (nombres de
  día/mes por idioma; zona inválida → `America/Bogota` con warning).
- La regla "usá esta fecha para hoy/mañana/el próximo X; nunca fechas pasadas" va en
  `core/playbooks/reglas-operacion.md` y en `packages/pack-calendar/playbooks/citas.md`
  (invariante 3: cero texto de instrucción al LLM en `.py`).
- El pack de calendario sigue calculando con `CalendarConfig.timezone`; el negocio debe mantener
  ambas iguales (se verifica al configurar el tenant).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Usar `CalendarConfig.timezone` | El core no puede depender de un pack (invariante 5); sin calendario no habría fecha |
| Usar `business_hours.tz` | Solo existe si hay rangos de atención; hoy se descarta en silencio sin ellos |
| Bloque con instrucciones armado en `prompt_builder.py` | Rompe la invariante 3 |
| Interpolar la fecha en un playbook estático | Cambia el prefijo cada minuto y rompe el prompt caching (`docs/PROMPT-CACHING-GUIDE.md:18-31`) |

## Consecuencias

**Positivas:** fechas relativas correctas en todos los packs, no solo en calendario; la instrucción
queda en el prefijo cacheable; una sola zona del negocio editable desde el panel.

**Negativas / lo que aceptamos pagar:** dos zonas que pueden divergir (negocio y pack de
calendario); el bloque dinámico ahora existe en todos los turnos (con Anthropic suma un
`cache_control`, ver riesgo de breakpoints en SPEC-24 `design.md`); un tenant que sobrescribe
`reglas-operacion.md` no recibe la regla nueva (por eso también va en `citas.md`).

**Qué haría que revisáramos esto:** un negocio con varias sedes en zonas distintas (Fase 2), o que
el E2E muestre fechas relativas mal calculadas con la fecha ya presente en el prompt.

## Impacto en el código

- `alembic/versions/0050_app_settings_timezone.py`, `core/db/models.py` (`AppSettings.timezone`).
- `core/api/routers/settings.py` (`IdentitySettings`/`IdentityUpdate`), `docs/openapi.json`.
- `core/llm/prompt_builder.py` (`PromptContext.timezone`, bloque de fecha), `core/orchestrator/loop.py:268-281`.
- `core/playbooks/reglas-operacion.md`; panel `configuracion/_components/identity-form.tsx`.
