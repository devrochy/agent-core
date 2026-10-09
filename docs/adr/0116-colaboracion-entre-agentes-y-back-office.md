# ADR-0116 — Colaboración entre agentes en un hilo y back-office asíncrono

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-18, HU-AOS-19, HU-AOS-20 y HU-AOS-30 (SPEC-26). Usa ADR-0112, ADR-0114, ADR-0113
  (`exposes.consultas`, límites del turno en el KernelSpec), ADR-0117 (el grafo va dentro de la release), ADR-0107
  (outbox), ADR-0108 (plantillas por evento), ADR-0122 (pausa) y ADR-0008 (`usage_ledger`).

## Contexto

- El Agent OS exige cinco modos de colaboración entre agentes (delegación, handoff, consulta, broadcast y colaboración).
- Cada modo necesita grafo de permisos, límites de profundidad, tiempo y presupuesto, trazabilidad total (de, a, intención, payload, resultado, tokens, costo, latencia) y aislamiento absoluto entre negocios.
- Hacerlo con agentes LLM independientes frente al cliente parte el contexto (ADR-0112).

## Decisión

Los agentes colaboran en el mismo hilo con mecanismos deterministas, permisos versionados y un ledger append-only; lo asíncrono corre en el worker y solo propone.

1. **Modos en el mismo hilo**, con un solo estado:
   - **Handoff:** cambio de dueño de la conversación, aplicado al cerrar el turno (dos fases). Las tareas viajan intactas.
   - **Delegación:** "agente como acción". `encargar(agente, procedimiento, entradas)` crea una sub-tarea del procedimiento del otro agente; si las entradas bastan, la completa el motor sin LLM; el resultado es tipado.
   - **Consulta:** lectura tipada que el otro agente declara en `exposes.consultas`, casi siempre sin LLM.
   - **Broadcast:** el Coordinador puntúa los disparadores de todos los agentes; el LLM solo desempata.
   - **Colaboración:** pila de tareas (una en foco y otra en pausa) resuelta en un solo mensaje, con una pregunta como máximo.

   Broadcast y colaboración son mecanismos internos del Coordinador, no llamadas entre agentes: no van en `agent_links.modes`; basta con que la instancia esté habilitada en la conexión.
2. **Permisos versionados:**
   - `a2a_graphs`, `a2a_graph_versions` y `agent_links`, con scope de negocio. El scope de conexión (con "un `deny` de conexión gana") se agrega cuando un negocio con dos conexiones pida permisos distintos.
   - Nadie llama al Coordinador.
3. **Guarda determinista** de cada operación:
   - permiso y contrato expuesto;
   - profundidad y ciclos (0);
   - repetición (misma operación y payload dos veces en un turno);
   - operaciones por turno y cambios de dueño (pasado el límite, se congela al dueño);
   - clases de datos;
   - presupuesto con reserva previa y liquidación posterior.

   Los límites tienen una sola fuente: los del turno en el KernelSpec (por defecto: profundidad 3, 6 operaciones por turno y más de 2 cambios de dueño en 6 turnos congela al dueño), los de cada enlace (modos, intenciones, tiempo, presupuesto, clases de datos) en `agent_links`, que solo puede bajarlos. La validación de la release exige `agent_links.max_depth` ≤ el límite del KernelSpec.
4. **Ledger:**
   - `a2a_events` es append-only, con payload redactado y su hash (nunca el payload completo). Se conserva hasta `purge_tenant`: una retención menor requiere un ADR que la declare como excepción a la invariante 8.
   - `a2a_conversations` es la proyección (estado, gasto, profundidad), actualizada en la misma transacción que el evento y reconstruible desde `a2a_events`.
   - La API arma una fila por llamada agrupando por `call_id`. No se crea una vista con `tenant_id` en `public`: `purge_tenant` recorre `information_schema.columns` y la trataría como tabla (`alembic/versions/0011_purge_tenant.py`).
   - Todas las tablas llevan `tenant_id`, RLS forzada con policy e índices que empiezan por `tenant_id`, y **toda** referencia a otra entidad del negocio (conexión, conversación, turno, instancia, versión) es **FK compuesta `(tenant_id, …)`**: un enlace o un evento entre negocios es imposible.
   - El gasto se registra en `usage_ledger` (ADR-0008) con `a2a_call_id` y cuenta para `costo_llm_usd` del plan (ADR-0045); `a2a_events` no es fuente de facturación.
   - Lo que vuelve de otro agente es dato validado contra un schema, nunca instrucción (invariante 7).
5. **Back-office:**
   - Son trabajos del worker disparados por el outbox (ADR-0107) o por crons (ADR-0044). Usan el LLM solo donde aporta (resumen, juez).
   - `core/backoffice/` solo da el runner: disparo, presupuesto, ledger y salida como propuesta. Los trabajos con lógica de dominio viven en su pack; el core no conoce pedidos.
   - Los prompts de los trabajos que usan el LLM viven en el AgentSpec del agente `kind = backoffice` o en playbooks del pack, nunca en `core/backoffice/` (invariante 3).
   - El presupuesto mensual es `policies.backofficeBudgetUsdMonthly` del KernelSpec (ADR-0113), versionado con la release (ADR-0117), y solo baja el tope `costo_llm_usd` del plan (ADR-0045).
   - **Solo proponen.** Una propuesta es un evento del outbox que el Coordinador de la conexión procesa como un turno sin mensaje entrante, con la release de la conversación (ADR-0117). Fuera de 24 h solo sale plantilla por evento (ADR-0108). Con la conexión pausada queda `failed`, sin reintento ni escalamiento (ADR-0122 §2), y el trabajo no corre ni gasta LLM sobre conexiones pausadas.
   - Los crons de packs que hoy escriben al cliente (`pack_commerce.jobs.follow_up_leads` y `follow_up_quotes`) pasan a proponer por este camino en HU-AOS-30. Hasta entonces son la excepción conocida.
   - Un agente de varios pasos con contexto propio se suma cuando un caso real lo exija.
6. La **federación entre negocios** queda fuera (SPEC-27): requiere doble consentimiento y un gateway propio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Agentes con contexto propio conversando frente al cliente | Inconsistencia por contexto partido |
| Cada operación como tarea de Celery | Suma latencia por salto y pierde el contexto del turno |
| Solo trazas OTel como registro | Retención y consultas insuficientes para auditoría; la facturación sigue en `usage_ledger` |
| Vista `a2a_calls` en `public` | Rompe `purge_tenant` para todos los negocios |

## Consecuencias

**Positivas:** colaboración trazable y acotada; cero bucles ejecutados por construcción; aislamiento entre negocios garantizado por el esquema.

**Negativas / lo que aceptamos pagar:** menos libertad para que un agente "converse" con otro; más tablas e índices; hay que calibrar los límites.

**Qué haría que revisáramos esto:** una tasa alta de denegaciones legítimas (los límites cortan casos válidos), un caso que exija contexto propio frente al cliente, o un trabajo de back-office que necesite varios pasos con contexto propio.

## Impacto en el código

`core/kernel/a2a.py` (guarda y operaciones); comandos `pasar_a_agente`, `encargar` y `consultar`; `core/backoffice/` (runner); `packages/pack-commerce/pack_commerce/jobs.py` (seguimientos como propuesta, HU-AOS-30); migraciones de permisos, ledger y proyección.
