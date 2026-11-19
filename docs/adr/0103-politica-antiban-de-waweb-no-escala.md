# ADR-0103 — El rechazo por política anti-baneo de `waweb` no escala ni pausa el bot

- **Estado:** aceptado
- **Fecha:** 2026-10-07
- **Enmienda:** ADR-0074 (canal `waweb`): el rechazo del connector por tope diario o pausa deja de viajar como
  fallo de envío recuperable con backoff; ahora difiere el mensaje sin marcarlo `failed` ni escalar.
- **Contexto:** issue #231; incidente del 2026-10-05 en test (tope diario 50/50 del negocio de Rob).

## Contexto

El connector de `waweb` consulta la política anti-baneo (`allow_send`) antes de cada envío y, si el tope diario o
la pausa están activos, responde `{"ok": false, "error": "envíos pausados o tope diario alcanzado"}`. Ese error
llegaba al worker como un `WaWebError` genérico → `GraphError` `TRANSIENT` → reintentos con backoff → al agotar
`max_retries`, `_mark_failed` marcaba el mensaje `failed` y llamaba `escalate(reason="send_failed")`: la
conversación quedaba **escalada y con el bot pausado**, y el cliente dejaba de recibir respuestas incluso después
de reiniciarse el tope. Un límite de diseño (anti-baneo, recuperable) apagaba al bot de forma permanente.

## Decisión

1. El connector marca el rechazo anti-baneo con `reason: "policy"` en el reply (`process_job`, `allow` en falso);
   sigue sin enviar.
2. `handoff.request_send` levanta `WaWebPolicyBlocked` (subclase de `WaWebError`) cuando ve `reason: policy`.
3. `outbound_send._send_one` la clasifica como `GraphError(category="POLICY")`; `decide_retry` devuelve
   `defer` — nunca backoff ni `fail_permanent`.
4. La tarea no toca el estado del mensaje (queda `queued`) ni escala: reprograma la misma tarea con
   `apply_async(eta=…)`:
   - justo después del reinicio del tope diario (00:02 UTC); o
   - `+15 min` si el canal está pausado a mano (kill-switch).
5. Cada mensaje soporta hasta `MAX_POLICY_DEFERRALS` (96, ~24 h) reprogramaciones; al agotarlas se deja constancia
   en la auditoría del negocio (`outbound_bloqueado_por_politica`) y no se encolan más tareas.

El escalado por `send_failed` queda reservado a fallos reales de envío (permanentes/transitorios agotados).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Marcar `failed` con motivo (sin escalar) | Pierde la respuesta y la cadena de la conversación queda bloqueada igual; el negocio no recupera el mensaje al reiniciarse el tope |
| Reintentar con el backoff normal de Celery | Es el bug: agota `max_retries` y cae en `fail_permanent` → DLQ + escalación |
| Barrido periódico cross-tenant de `queued` | Correcto a largo plazo, pero exige una función `SECURITY DEFINER` nueva (migración) y otra puerta de datos cross-tenant; la reprogramación por ETA cubre el caso sin migración |
| Colchón de gracia configurable en el panel | Es el alcance de #232 (tope y pausa configurables, aviso al negocio, recarga sin reiniciar); este ADR solo quita la pausa falsa |

## Consecuencias

**Positivas:** un tope diario ya no apaga el bot; al reiniciarse el tope la conversación sigue atendida sin
intervención. El rechazo es barato (el connector ni envía), así que reprogramar no arriesga la sesión.

**Negativas / lo que aceptamos pagar:**
- Si la pausa (kill-switch) dura más de 24 h, el mensaje queda en cola y la cadena de esa conversación no avanza
  hasta que se reactive el canal; hoy no hay disparador de reanudación (lo cubrirá #232 con "recarga sin reiniciar").
- El contador de reprogramaciones vive en Redis con TTL de 2 días; si Redis se pierde, el mensaje puede
  reprogramarse de nuevo (inofensivo: el connector vuelve a rechazarlo mientras siga bloqueado).

**Qué haría que revisáramos esto:** #232 (controles y aviso desde el panel), o un canal con límite por hora en vez
de por día.

## Impacto en el código

- `core/waweb/outbound.py::process_job`: `reason: "policy"` en el reply del rechazo.
- `core/waweb/handoff.py`: `WaWebPolicyBlocked` y su rama en `request_send`.
- `core/gateway/meta_errors.py`: categoría `POLICY` en `ErrorCategory`.
- `core/tasks/outbound_send.py`: `RetryDecision("defer")`, rama `POLICY` en `decide_retry`, captura en `_send_one`,
  `_bump_policy_deferrals`, `_policy_eta` y `_handle_policy_defer`.
- Tests: `tests/test_waweb_connector.py`, `tests/test_waweb_handoff.py`, `tests/test_outbound_send_task.py`.
