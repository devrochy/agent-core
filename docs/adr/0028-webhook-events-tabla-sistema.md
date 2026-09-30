# ADR-0028 — `webhook_events` como tabla de sistema (excepción a invariante nº1)

- **Estado:** aceptado
- **Fecha:** 2026-09-19

## Contexto

Se agrega `webhook_events`, un log crudo de entregas de webhook de Meta para
auditoría (`callback_url`, firma válida o no, payload, reintentos). Un
webhook puede llegar **antes** de poder resolver a qué tenant pertenece
(firma HMAC inválida, `app_id` todavía no vinculado a ningún tenant en
`meta_connections`) — son precisamente los casos que este log existe para
auditar. La invariante nº1 de `CLAUDE.md` exige `tenant_id uuid not null` +
RLS en toda tabla de negocio.

## Decisión

`webhook_events` es una tabla de **sistema** (misma categoría que
`admins`/`plugins`, ver docstring de `core/db/models.py`), no de negocio:
`tenant_id` es nullable y la tabla no lleva RLS. Exigir `tenant_id not null`
volvería inútil su propio propósito (no podría registrar los eventos que
todavía no tienen tenant resuelto).

**Actualización 2026-09-20**: el pipeline real ya escribe acá
(`core/gateway/webhooks.py::_log_webhook_event`, único punto de escritura de
código de aplicación — verificado por `tests/test_webhook_events_solo_desde_webhooks.py`,
mismo patrón que `tests/test_meta_token_store_aislado.py`). Todo acceso
(lectura desde el panel admin incluida, si se agrega en el futuro) debe
filtrar `tenant_id` a mano en cada query, sin confiar en RLS para el
aislamiento (no lo hay). No hay ninguna lectura desde el panel admin todavía.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `tenant_id not null` con un tenant "sistema" reservado como sentinel | Ensucia el espacio de tenants reales con una fila mágica; cualquier código que itere tenants tendría que excluirla a mano — más frágil que `NULL` explícito. |
| RLS con policy que permita `tenant_id is null OR tenant_id = current_setting(...)` | Permitiría que CUALQUIER tenant autenticado vea las filas sin tenant resuelto (que pueden contener payloads de otros tenants aún no vinculados) — peor que no tener RLS y ser explícito sobre el riesgo. |

## Consecuencias

**Positivas:** el log puede auditar exactamente los casos límite (webhooks
huérfanos, firmas inválidas) que son el motivo de que exista.

**Negativas / lo que aceptamos pagar:** sin RLS, un futuro `select(WebhookEvent)`
sin filtro explícito de `tenant_id` mezclaría payloads de varios tenants —
"puerta abierta" documentada (hallazgo de `auditor-multitenant`, 2026-09-19)
que hoy no tiene consecuencia real porque nada lee/escribe la tabla todavía.

**Cumplido (2026-09-20)**: el pipeline ya escribe acá, con el test estático
prometido ya agregado. **Qué haría que revisáramos esto de nuevo:** el día
que se agregue una lectura desde el panel admin (ej. para debug de webhooks
fallidos) — en ese momento, exigir `tenant_id` explícito en el `WHERE` de
esa vista, con su propio test que lo verifique (no alcanza con el test
estático de "quién importa el modelo", hay que probar el filtro real).

## Impacto en el código

`alembic/versions/0017_webhook_events.py`, `core/db/models.py`
(`WebhookEvent`), `core/gateway/webhooks.py::_log_webhook_event` (único
punto de escritura), `tests/test_webhook_events_solo_desde_webhooks.py`.
