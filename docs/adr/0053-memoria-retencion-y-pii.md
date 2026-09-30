# ADR-0053 — Memoria: retención, purga por cliente y redacción de PII

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

La memoria guarda contenido crudo de conversaciones en `memory_vectors` (pgvector) y
en la working memory (Redis), sin política de **retención** ni forma de **borrar la
memoria de un cliente** (GDPR/"derecho al olvido") ni de **redactar datos sensibles**
antes de embeber. `purge_tenant` borra todo al eliminar el tenant, pero no hay nada
por debajo de ese nivel. El `Vector(384)` está fijo y los embeddings son globales por
`EMBEDDING_PROVIDER` (deuda ya anotada en `BACKLOG.md`).

## Decisión

1. **Redacción de PII antes de embeber.** `store_memory` pasa el contenido por
   `redact_pii()`, que enmascara emails, teléfonos y secuencias tipo tarjeta
   (patrones amplios, **sin** regex catastrófica). Es determinista y best-effort: la
   fuente de verdad (el mensaje) queda intacta en `messages`; lo que se embebe es la
   versión redactada.
2. **Purga de memoria por cliente/conversación.** `purge_memory(customer_id=…,
   conversation_id=…)` marca `deleted_at` de los `memory_vectors` correspondientes
   (borrado **lógico**, invariante #8) y limpia la working memory de Redis de esas
   conversaciones. Expuesta por CLI (`agent-core tenant memoria-purgar`) para
   cumplir un pedido de "olvidá a este cliente".
3. **Retención por TTL.** `MEMORY_RETENTION_DAYS` (config, default 0 = sin
   retención): un cron (`core.tasks.memory_retention`) purga lógicamente la memoria
   más vieja que el TTL. La **working memory** ya expira sola (TTL Redis 24h).
4. **Fuera de alcance (documentado):** cambiar la dimensión de `Vector(384)` o
   embeddings **por tenant (BYOK)** requiere migración de esquema + proveedor por
   tenant; se difiere (queda en BACKLOG) para no arriesgar el recall existente.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Borrado físico de memoria | Contradice la invariante #8 (soft-delete) salvo `purge_tenant`. |
| Redacción con regex "perfecta" | No existe; se usan patrones amplios y el mensaje fuente sigue en `messages` para auditoría. |
| Embeddings por tenant ahora | Migración de dimensión + claves por proveedor; alto riesgo, sin caso de uso urgente. |
| Retención a nivel de conversación (messages) | Es otro alcance (retención de negocio completa); acá se cubre la **memoria** semántica. |

## Consecuencias

**Positivas:** se puede olvidar a un cliente sin borrar toda su historia de negocio;
la memoria no retiene PII evidente; la retención es configurable por despliegue.

**Negativas / lo que aceptamos pagar:** la redacción reduce precisión del recall en
esos casos (trade-off deliberado); `MEMORY_RETENTION_DAYS=0` por defecto (sin purga
automática hasta que el operador lo configure); embeddings/dimensión siguen globales.

**Qué haría que revisáramos esto:** que se exija embeddings BYOK por tenant, o que la
retención deba cubrir `messages`/`customers` completos (no solo memoria).

## Impacto en el código

`core/memory/vector.py` (`redact_pii`, `purge_memory`), `core/config.py`
(`MEMORY_RETENTION_DAYS`), `core/tasks/memory_retention.py` (cron), `cli/commands/
tenant.py` (subcomando de purga), `docs/BACKLOG.md`. Sin migración (usa `deleted_at`
existente). Tests unitarios de redacción y purga.

## Corrección post-auditoría

- **PII persistida**: `store_memory` guarda en `content` la versión **redactada** (no
  solo la embebe redactada), para no dejar PII en `memory_vectors`. El mensaje sin
  redactar sigue en `messages` (fuente de verdad/auditoría). Sin ReDoS.
