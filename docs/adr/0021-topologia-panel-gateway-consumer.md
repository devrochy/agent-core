# ADR-0021 — Topología de despliegue: panel y gateway+consumer en un solo proceso

- **Estado:** aceptado
- **Fecha:** 2026-08-28

## Contexto

El diagrama C4 (`docs/architecture/c4.md`) expone que `core-api` (un solo proceso
FastAPI) sirve el panel SSR, recibe los webhooks de Meta, y además consume el stream
de mensajes, hace el debounce y recupera conversaciones huérfanas. Un query pesado
del panel compite por el event loop con la latencia del webhook y con el turno del
consumidor. Existen dos subsistemas de cola (consumer propio en el API + Celery para
crons).

## Decisión

**Hoy: monolito en un proceso** (invariante de diseño: ADR-0011 monolito modular).
No se separa. La compartición del event loop se mitiga con lo que ya existe: colas
con cuota por tenant (Redis streams), semáforos por tenant (`turn_gate`) y tasks
lentas del panel ejecutadas con await explícito.

**Cuándo sí separar (criterio de disparo):** cuando la telemetría (OTel + usage_ledger)
muestre que una vista del panel o una carga masiva degrada el p99 del webhook por
encima del umbral operativo, o cuando el consumo de memoria del panel compita con el
consumer. En ese momento la topología se parte SIN cambiar el monolito de código:

- `panel` (core-api solo SSR del panel) y `gateway+consumer` (webhooks + consumer +
  debounce + recovery) como dos contenedores del mismo binario (`core/main.py`
  parametrizado por `AGENT_CORE_ROLE`).
- El consumer puede moverse al worker (Celery) si el uso lo justifica, unificando
  los dos subsistemas de cola.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Separar hoy | Complejidad operativa sin evidencia de degradación; el monolito es la decisión del framework (ADR-0011) |
| Mover el consumer al worker ya | El worker (Celery prefork) no corre el loop asyncio compartido; el consumer vive mejor en el proceso con el contexto web |

## Consecuencias

**Positivas:** una sola imagen, una sola configuración de despliegue; la personalización
por cliente usa la palanca de topología (invariante #10) solo cuando hay evidencia.

**Negativas / lo que aceptamos pagar:** el panel puede degradar la latencia del
webhook en cargas altas; la métrica operativa (p99 del webhook) debe existir para
disparar la separación a tiempo.

**Qué haría que revisáramos esto:** mediciones de OTel que muestren p99 del webhook
degradado, o un incidente de latencia causado por el panel.