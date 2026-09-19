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

Hoy la tabla existe pero **ningún código la usa** (el pipeline real en
`core/gateway/webhooks.py` sigue sin escribirla) — es infraestructura para
un trabajo futuro de conectar el log real. Cuando eso pase, todo acceso
(lectura desde el panel admin incluida) debe filtrar `tenant_id` a mano en
cada query, sin confiar en RLS para el aislamiento (no lo hay).

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

**Qué haría que revisáramos esto:** el día que `core/gateway/webhooks.py`
empiece a escribir en `webhook_events` de verdad — en ese momento, antes de
exponer cualquier lectura desde el panel admin, agregar un test estático
(mismo patrón que `tests/test_meta_token_store_aislado.py`) que obligue a
que todo acceso pase por un helper único con `tenant_id` explícito en el
`WHERE`.

## Impacto en el código

`alembic/versions/0017_webhook_events.py`, `core/db/models.py`
(`WebhookEvent`). Sin impacto en `core/gateway/webhooks.py` todavía (no
conectado).
