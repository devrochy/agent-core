# ADR-0105 — Bloqueo progresivo de inicio de sesión (panel/API y API de campo)

- **Estado:** aceptado
- **Fecha:** 2026-10-07
- **Contexto:** HU-ARCH-04 (D-SPEC-01-3): endurecer el acceso al panel, la API y la API de campo.

## Contexto

El login de la plataforma no tenía ninguna defensa contra fuerza bruta: tanto `POST /api/v1/auth/login`
(identidad admin, sin tenant) como `POST /auth/gestor/login` de la API de campo aceptaban intentos
ilimitados. El panel da acceso a los datos de todos los clientes de un negocio y el master a todos los
negocios: un acceso indebido es el peor incidente posible para una plataforma multi-tenant.

## Decisión

1. **Regla única en `core/security/login_lockout.py`**: 5 intentos fallidos del mismo **identificador** o de
   la misma **IP** en 10 minutos → rechazo de 15 minutos con mensaje neutro (`429`, código `auth_bloqueado` /
   `"demasiados intentos, espera unos minutos"`).
2. **Estado en Redis con TTL** (se autolimpia): contadores `auth:fail:<scope>:…` y bloqueos
   `auth:block:<scope>:…`. Si Redis no responde, el login **no se bloquea** (fail-open explícito): un Redis
   caído no puede tumbar el acceso legítimo, y el bloqueo es una capa complementaria a la revocación de sesión.
3. **`scope` por dominio y por negocio**: `admin` para el login de identidad; `gestor:<slug>` para la API de
   campo, de modo que los fallos de un negocio nunca bloquean el mismo usuario en otro (aislamiento por negocio).
4. **El éxito limpia** los fallos acumulados del identificador y de la IP.
5. **Auditoría**: en la API de campo (tenant conocido) el disparo del bloqueo queda en `audit_log`
   (`actor=core:auth`, `action=login_bloqueado`). En el login de identidad (sin tenant todavía) queda el evento
   estructurado `auth_login_blocked` en los logs y el estado en Redis; mover ese evento a `audit_log` exigiría
   una tabla de plataforma sin `tenant_id` (fuera del alcance de esta HU).
6. **IP del cliente detrás del proxy**: se lee `X-Forwarded-For` (primer valor) o `CF-Connecting-IP` antes del
   socket. Detrás del BFF del panel el mecanismo confiable es el **identificador**; la IP es defensa adicional
   para accesos directos a la API.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Bloquear solo por IP | El BFF del panel y Cloudflare comparten la IP de origen; bloquearía a todos los usuarios legítimos tras un ataque |
| Bloquear solo por identificador | Un atacante que rota usuarios esquiva el bloqueo; la IP agrega la segunda red |
| Tabla en Postgres para los intentos | Escrituras por cada login fallido y limpieza manual; Redis con TTL es el mismo estado sin migración |
| `audit_log` con `tenant_id` nulo | La invariante de tenant NOT NULL y el hook de migraciones la rechazan; una tabla de plataforma merece su propio ADR |

## Consecuencias

**Positivas:** el sexto intento de un atacante se rechaza 15 minutos con mensaje neutro (sin filtrar si la
cuenta existe); los negocios no se bloquean entre sí; sin Redis el acceso legítimo nunca se cae.

**Negativas / lo que aceptamos pagar:**
- Un atacante distribuido (IPs distintas) con usuarios distintos no se bloquea hasta agotar los identificadores;
  la mitigación complementaria es la observabilidad de `auth_login_blocked` y, más adelante, Turnstile.
- Detrás de un NAT grande, la cuota por IP puede bloquear antes de los 5 fallos reales; el contador por
  identificador es el camino principal.
- La auditoría del dominio `admin` es log-based hasta que exista una tabla de eventos de plataforma.

**Qué haría que revisáramos esto:** [HU-SH-07](../03-fase-0-go-live/SPEC-07-asistente-solvershub/hu/HU-SH-07-antiabuso-y-cumplimiento.md)
(Turnstile y antiabuso del alta automática) o la tabla de eventos de plataforma.

## Impacto en el código

- `core/security/login_lockout.py` (nuevo): contadores, bloqueos, `client_ip`.
- `core/api/routers/auth.py::post_login`: chequeo previo, conteo de fallos y limpieza en el éxito.
- `packages/pack-cobranza/pack_cobranza/api_gestor.py::_login_con_tenant`: mismo ciclo con scope `gestor:<slug>`
  y auditoría por tenant.
- Tests: `tests/test_login_lockout.py` (12) y los existentes de auth/gestor siguen en verde.
