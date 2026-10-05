# ADR-0092 — Fallo de guardrail con respuesta neutral configurable

- **Estado:** aceptado
- **Fecha:** 2026-10-04
- **Relacionado:** ADR-0050 (persona y guardrails del spec), ADR-0051 (autoescalado por turnos sin resolver)
- **Contexto:** HU-CAL-04 (SPEC-24, `solvershub-docs`), decisión D-SPEC-24-7

## Contexto

Cuando una respuesta falla un guardrail y se agotan los reintentos, el loop siempre escala: pausa el
bot y crea una fila en `escalations` (`core/orchestrator/loop.py:500-514`), con un solo reintento por
defecto (`core/orchestrator/guardrails.py:25`). En la prueba con clientes reales hubo 11
escalamientos por `calendar_completion_check` en conversaciones que un reintento más o una respuesta
prudente habrían resuelto.

## Decisión

Cada negocio elige qué pasa al agotar los reintentos del guardrail con
`behavior_config.guardrail_fail_action`: `escalate` (default, comportamiento actual) o `neutral`.

Con `neutral`:
1. No escala ni pausa el bot. Responde la sección `verifying` de `core/playbooks/respuestas.md`
   ("Dejame verificar eso y te confirmo en un momento, por favor.", con constante de respaldo) más
   los `_safe_append` del turno (datos que el servidor ya verificó).
2. El borrador que falló nunca se envía.
3. Registra `audit_log` `guardrail_neutral` con el id del guardrail (sin texto de la respuesta).
4. Cierra el turno con `_finish_turn(resolved=False)`: suma a `turnos_sin_resolver`, y el
   autoescalado de ADR-0051 (`auto_escalate_after_turns`) sigue siendo la red.

Un valor desconocido se trata como `escalate`. Se expone en Configuración → Identidad junto a
`guardrail_max_retries`, que pasa a validarse 0-5 en la API igual que en el loop.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Siempre escalar (hoy) | Pausa el bot por fallos que no lo merecen; cuesta demos y tiempo humano |
| Nunca escalar | Un fallo persistente deja al cliente sin salida |
| Enviar el borrador que falló | Es justo lo que el guardrail existe para impedir (confirmaciones falsas) |

## Consecuencias

**Positivas:** menos escalamientos innecesarios; el negocio controla el trade-off; el default no
cambia para nadie; la falla queda medible en `audit_log`.

**Negativas / lo que aceptamos pagar:** el cliente recibe "déjame verificar" y nadie hace seguimiento
proactivo hasta su siguiente mensaje o el autoescalado; el autoescalado cuenta como no resuelto todo
turno sin tools, así que un umbral bajo puede escalar charlas normales.

**Qué haría que revisáramos esto:** que `guardrail_neutral` aparezca en más del 5 % de los turnos de
un negocio, o quejas de clientes que quedaron esperando una verificación; en ese caso, aviso interno
al equipo sin pausar el bot.

## Impacto en el código

- `core/orchestrator/loop.py` (salida neutral, helper de autoescalado compartido), `core/orchestrator/escalation.py` (`VERIFYING_REPLY`).
- `core/playbooks/respuestas.md` (`## verifying`).
- `core/api/routers/settings.py` (`guardrail_fail_action`, validación de `guardrail_max_retries`), `docs/openapi.json`; panel `identity-form.tsx`.
- Tests: `tests/integration/test_guardrail_retries_per_tenant.py`, `tests/test_core_tools.py`.
