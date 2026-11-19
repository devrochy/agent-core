# Plan — Agendamiento definitivo (SPEC-24 · HU-CAL-04/05/06)

- **Estado:** aprobado (documentación lista; implementación por hacer)
- **Fecha:** 2026-10-04
- **Fuente de los contratos:** `solvershub-docs/04-fase-1-competir/SPEC-24-agenda-y-citas/design.md`
  (sección "Ampliación"). Este plan **no** los repite: dice en qué orden, con qué archivos y con qué
  comandos se ejecuta cada HU.
- **ADRs:** [0091](../adr/0091-fecha-actual-y-zona-horaria-del-negocio.md) ·
  [0092](../adr/0092-fallo-de-guardrail-neutral-configurable.md)

## Objetivo

Que el asistente agende en la fecha correcta, solo ofrezca horarios futuros, confirme únicamente lo
que la tool confirmó, mande el link de la cita y no escale por fallos que un reintento resuelve.
Origen: la prueba en test con dos prospectos (2026-10-04/05), 9 fallas listadas en la spec.

## Decisiones cerradas (D-SPEC-24-5 a 10)

- Zona horaria por negocio en `app_settings.timezone` (default `America/Bogota`, migración 0050).
- Fecha actual como dato al final del bloque dinámico; la instrucción en playbooks (invariante 3).
- `behavior_config.guardrail_fail_action` = `escalate` (default) | `neutral`.
- Guardrail de calendario con evidencia por `status` real del turno.
- Link de cita = URL de plantilla de Google Calendar, también en modo interno y en el correo.
- Panel: correo + WhatsApp al mover o cancelar con `notify` (paridad con el panel legado).

## Ejecución por HU (una HU = un issue = una rama = un PR por repo)

### HU-CAL-04 · Fecha actual y anti-escalamiento — rama `feat/hu-cal-04-fecha-y-anti-escalamiento`

| Paso | Archivos |
|---|---|
| Migración (skill `db-migration`) | `alembic/versions/0050_app_settings_timezone.py` (patrón `0045_app_settings_currency.py`), `core/db/models.py` |
| API | `core/api/routers/settings.py` (`timezone`, `guardrail_fail_action`, validación 0-5) → `scripts/export_openapi.py` |
| Prompt | `core/llm/prompt_builder.py` (`PromptContext.timezone`/`now`, bloque de fecha al final de `dynamic_parts`), `core/orchestrator/loop.py:268-281` |
| Playbooks | `core/playbooks/reglas-operacion.md` (fecha + anti-filtración), `core/playbooks/respuestas.md` (`## verifying`) |
| Modo neutral | `core/orchestrator/loop.py:500-539` (salida neutral + helper de autoescalado), `core/orchestrator/escalation.py` (`VERIFYING_REPLY`) |
| Tests | `tests/test_agent_spec.py`, `tests/test_core_tools.py`, `tests/integration/test_guardrail_retries_per_tenant.py` (+ casos de API y migración) |
| Panel (repo `solvershub-panel`, base `main`) | `apps/panel-web/app/(app)/configuracion/_components/identity-form.tsx` + `make api-types` |

### HU-CAL-05 · Pack de agendamiento definitivo — rama `feat/hu-cal-05-pack-agendamiento`

| Paso | Archivos |
|---|---|
| Tools (skill `new-tool`) | `packages/pack-calendar/pack_calendar/skills.py`: `in_past` y filtro de horas pasadas en `check_availability`, `_day_free_slots`, `find_next_available`, `_calendar_link`, `{link}` y formato seguro en plantillas |
| Config | `pack_calendar/config.py` (defaults de correo con `{link}`, descripción de `completion_claims_disabled`) |
| Guardrail | `pack_calendar/module.py` (patrones afirmativo/negativo y evidencia por `status`) |
| Panel API | `pack_calendar/admin_api.py` (PATCH y cancel con correo + WhatsApp + `notification`; cancel registra `cancelada`) → OpenAPI |
| Prompts | `packages/pack-calendar/playbooks/citas.md` (texto definitivo), `packages/pack-calendar/flow.json` |
| Tests | `tests/test_calendar_slots.py`, `tests/test_pack_calendar_completion_guardrail.py`, `tests/test_pack_calendar_claims_dsl.py`, `tests/integration/test_calendar_interno.py` (cross-tenant), golden en `evals/golden_scenarios/` |
| Docs | `packages/pack-calendar/CALENDAR.md`, `CHANGELOG.md` |
| Panel | `apps/panel-web/app/(app)/calendario/citas/_components/appointment-panel.tsx` (resultado real del aviso) |

### HU-CAL-06 · Afinado del tenant y E2E — sin rama de código

Configuración del tenant de test, artefacto del volumen, SMTP, E2E de 10 casos y cierre documental
(ver la HU y el runbook de abajo).

## Comandos de verificación (cada PR de backend)

```bash
uv run --extra dev python -m ruff check core packages tests alembic scripts
PYTHONPATH="$(printf '%s:' packages/pack-*/ packages/contracts)" .venv/bin/lint-imports
uv run --extra dev python -m pytest tests -q --ignore=tests/integration
uv run --extra dev python scripts/export_openapi.py   # si cambió /api/v1
```

Integración (stack local: Postgres `localhost:5433`, Redis `localhost:6380`). El consumer de
`agent-core-s1-core-api-1` compite por el stream: detenerlo antes y arrancarlo después.

```bash
docker stop agent-core-s1-core-api-1
AGENT_CORE_INTEGRATION=1 uv run --extra dev python -m pytest tests/integration/test_calendar_interno.py tests/integration/test_guardrail_retries_per_tenant.py -q
docker start agent-core-s1-core-api-1
```

Deploy: skill `desplegar-test-agent-core` (backend, verifica `alembic_version`, RLS con FORCE,
healthz y login) y `desplegar-test` (panel). El token de Coolify se le pide al usuario y no se guarda.
Producción (`ggzjdyc7zsemt6kzafx5rg7m`) no se toca.

## Runbook del E2E (HU-CAL-06)

1. **Respaldo del artefacto** del volumen `tr0vpdei5av6szcgwpe1vwcg-tenants-data` (`tar` de
   `solvershub/`). Editar en el host y escribir de vuelta con `docker run --rm -i -v …:/data alpine sh -c 'cat > …'`.
   Los artefactos se leen en cada turno (`core/orchestrator/loop.py:253`).
2. **Conversaciones de prueba** (tenant `e46e57e7-b3c5-4f5a-9112-0382b2365518`): `e0d056df-494c-4435-9e5e-eeb0172ce5e9`
   y `2e86a595-3972-4ae9-84b7-e05eb9178d44`. Si están `escalated`/`bot_paused`, reabrir con
   `update conversations set status='open', bot_paused=false where id='…'`.
3. **Inbound**: `xadd` a `agent_core:inbound` con `tenant_id`, `provider=waweb`, `channel=whatsapp`,
   `external_message_id` único, `customer_external_id`, `customer_name`, `content`,
   `connection_external_id='waweb:<tenant>'`, `media='[]'`, `media_context=''`.
4. **Esperar ~50 s** y leer `messages`, `audit_log` y `escalations` de la conversación; pegar la
   evidencia de cada caso en la HU-CAL-06.
5. Login del API de test: usuario del seed `scripts/seed_demo_login.py` con `X-Tenant-Id` del tenant.
