# ADR-0164 — Servicio de embeddings para más de un pack

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-40, P-32 (opción A). **Enmienda:** ADR-0145 §3 (el servicio deja de ser solo de conocimiento). **Usa:** ADR-0145 §1 y §2.

## Contexto

`core/llm/embeddings.py` está atado a pack-knowledge: `embeber_conocimiento(..., document_id)`, el setting `KNOWLEDGE_EMBEDDING_PROVIDER` y el ledger con `result={"proposito": "ingesta", "document_id": …}`. La búsqueda semántica de la vertical (HU-INMO-40) necesita embeber inmuebles y consultas con el mismo proveedor, dimensión y medición. Con esta, el servicio tiene dos consumidores (ADR-0023).

## Decisión

El servicio se generaliza **sin cambiar** su conducta para conocimiento.

1. Nueva función `embeber(textos, *, proposito, ref: RefEmbedding)`, con `RefEmbedding = {pack, ref}`. `embeber_conocimiento` queda como envoltura que pasa `{pack: "pack_knowledge", ref: document_id}`.
2. El setting se renombra a `EMBEDDING_PROVIDER`; se acepta `KNOWLEDGE_EMBEDDING_PROVIDER` como alias durante una versión.
   - Se mantienen el modelo `text-embedding-3-small`, `DIMENSION = 1536` y `hash` fuera de producción.
3. **Ledger:**
   - una fila `kind='embedding'` por lote de indexación, con `result={"proposito": "ingesta", "pack": …, "ref": …}`;
   - la consulta no escribe fila, igual que en ADR-0145 §3.
4. **Uso desde la vertical:**
   - similitud exacta (`embedding <=> :q`) sobre los ≤ 200 candidatos que ya pasaron los filtros, leídos por PK;
   - **sin HNSW** (ADR-0145 §1);
   - la indexación es una tarea `register_task` con `tenant_scope`.
5. **Evaluación con embeddings grabados.** El golden de relevancia se mide con el context manager `grabacion`. Grabar con el proveedor real necesita el OK del dueño con la estimación de costo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Copiar el servicio dentro del pack inmobiliario | Duplica proveedor, medición y llave de plataforma, y se separan con el tiempo |
| Índice HNSW por negocio | No se puede encabezar por `tenant_id`: la RLS filtra después y un negocio chico recibe menos de k resultados (ADR-0145 §1) |

## Consecuencias

**Positivas:** un solo servicio de embeddings con costo medido para cualquier pack que lo necesite.

**Negativas / lo que aceptamos pagar:** un alias de setting por una versión y una envoltura más.

**Qué haría que revisáramos esto:** necesitar otra dimensión o un modelo por pack, lo que pediría `dim` por consumidor y re-embebido.

## Impacto en el código

- `core/llm/embeddings.py`.
- `core/config.py` (alias del setting).
- `packages/pack-knowledge` (sin cambios de conducta).
- Pack inmobiliario (tabla `inmueble_vectores` y tarea de indexación).
