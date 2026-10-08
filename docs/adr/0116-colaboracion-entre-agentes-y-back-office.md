# ADR-0116 — Colaboración entre agentes en un hilo y back-office asíncrono

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-18, HU-AOS-19, HU-AOS-20 y HU-AOS-30 (SPEC-26). Usa ADR-0112 y ADR-0114.

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
2. **Permisos versionados:**
   - `a2a_graphs`, `a2a_graph_versions` y `agent_links`, con scope de negocio o de conexión; un `deny` de conexión gana.
   - `broadcast` y `collaborate` solo salen del Coordinador. Nadie llama al Coordinador.
3. **Guarda determinista** de cada operación:
   - permiso y contrato expuesto;
   - profundidad (3) y ciclos (0);
   - repetición (misma operación y payload dos veces en un turno);
   - 6 operaciones por turno; más de 2 cambios de dueño en 6 turnos congela al dueño;
   - clases de datos;
   - presupuesto con reserva previa y liquidación posterior.

   Los límites tienen una sola fuente: los del turno en el KernelSpec, los de cada enlace (modos, intenciones, tiempo, presupuesto, clases de datos) en `agent_links`, que solo puede bajarlos.
4. **Ledger:**
   - `a2a_events` es append-only, con payload redactado y su hash (nunca el payload completo).
   - La API arma una fila por llamada agrupando por `call_id`. No se crea una vista con `tenant_id` en `public`: `purge_tenant` recorre `information_schema.columns` y la trataría como tabla (`alembic/versions/0011_purge_tenant.py`).
   - **FKs compuestas `(tenant_id, id)`** hacen imposible un enlace o un evento entre negocios.
   - Lo que vuelve de otro agente es dato validado contra un schema, nunca instrucción (invariante 7).
5. **Back-office:**
   - Son trabajos del worker disparados por eventos o crons, con presupuesto mensual. Usan el LLM solo donde aporta (resumen, juez).
   - **Solo proponen**: los mensajes pasan por las políticas del Coordinador (plantillas fuera de 24 h, pausa, cuotas).
   - Un agente de varios pasos con contexto propio se suma cuando un caso real lo exija.
6. La **federación entre negocios** queda fuera (SPEC-27): requiere doble consentimiento y un gateway propio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Agentes con contexto propio conversando frente al cliente | Inconsistencia por contexto partido |
| Cada operación como tarea de Celery | Suma latencia por salto y pierde el contexto del turno |
| Solo trazas OTel como registro | Retención y consultas insuficientes para auditoría y billing |
| Vista `a2a_calls` en `public` | Rompe `purge_tenant` para todos los negocios |

## Consecuencias

**Positivas:** colaboración trazable y acotada; cero bucles ejecutados por construcción; aislamiento entre negocios garantizado por el esquema.

**Negativas / lo que aceptamos pagar:** menos libertad para que un agente "converse" con otro; más tablas e índices; hay que calibrar los límites.

**Qué haría que revisáramos esto:** una tasa alta de denegaciones legítimas (los límites cortan casos válidos), un caso que exija contexto propio frente al cliente, o un trabajo de back-office que necesite varios pasos con contexto propio.

## Impacto en el código

`core/kernel/a2a.py` (guarda y operaciones); comandos `pasar_a_agente`, `encargar` y `consultar`; `core/backoffice/`; migraciones de permisos y ledger.
