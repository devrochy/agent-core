# ADR-0012 — Autenticación y contexto de tenant sin Supabase

- **Estado:** aceptado
- **Fecha:** 2026-08-26
- **Reemplaza:** la mención del "JWT de Supabase (claim tenant_id)" de ADR-0001

## Contexto

ADR-0001 citaba el claim `tenant_id` del JWT de Supabase. El runtime no usa Supabase:
Postgres/Redis locales, sesiones de panel con cookie + bcrypt y webhooks validados por firma
HMAC de Meta. Introducir Supabase añadiría una dependencia externa y una base de datos remota
a un despliegue self-hosted, sin consumidores que lo justifiquen.

## Decisión

El `tenant_id` se resuelve del **contexto autenticado**, nunca del cuerpo de la petición
(invariante del auditor punto 7):

- **Panel:** cookie + bcrypt → admin → tenant(s) vía `tenant_user`; un middleware establece
  el contextvar y ejecuta `set_config('app.tenant_id', $1, true)` (scope de **transacción**)
  al inicio de cada petición.
- **Webhooks:** `phone_number_id` → `channel_connection` → tenant vía función
  `SECURITY DEFINER` (`resolve_tenant_for_phone`), exenta de RLS por ser el bootstrap
  chicken-and-egg.
- **Worker/debounce/Celery:** tenant del campo del stream / del payload del turno.
- Las policies de RLS leen `current_setting('app.tenant_id')`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Supabase Auth + JWT | Dependencia externa y BD remota; el runtime es self-hosted |
| `tenant_id` en el cuerpo de la petición | Falsificable; contradice el punto 7 del auditor |
| Rol/connection por tenant | Complejidad ×N sin beneficio hasta que un tenant lo exija |

## Consecuencias

**Positivas:** sigue self-hosted; el tenant viene de un contexto de confianza; `set_config`
con `true` evita que el contexto se filtre entre requests del pool.

**Negativas / lo que aceptamos pagar:** todo punto de entrada debe establecer el contexto
(disciplina permanente); las funciones `SECURITY DEFINER` son una puerta de escape de RLS y
deben ser mínimas y auditadas.

**Qué haría que revisáramos esto:** que adoptemos un auth gestionado (Clerk/Supabase) o que
la resolución `SECURITY DEFINER` crezca más allá de dos funciones — señal de que el modelo de
contexto está mal trazado.

## Impacto en el código

`core/db/session.py`, `core/shared/tenant.py` (nuevo), middleware de `core/main.py`,
`core/gateway/connections.py`, `core/gateway/webhooks.py`, `core/orchestrator/consumer.py`,
`core/orchestrator/debounce.py`, `docs/ROADMAP.md:29`.