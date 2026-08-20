# ADR-004 · Memoria en 3 capas + proveedor de embeddings configurable

**Estado**: Aceptada · **Fecha**: 2026-08-19

## Contexto

agent-sale instaló pgvector desde la migración 0001 y NUNCA lo usó: no eligieron
proveedor de embeddings (DeepSeek no tiene API de embeddings). agent-dashboard no
tiene memoria vectorial. El gap es real y documentado.

## Decisión

Tres capas con responsabilidades y modos de fallo distintos:

| Capa | Storage | Fallo |
|---|---|---|
| L1 Working (últimos 30 turnos) | Redis LIST, TTL 24h | Degrada a Postgres |
| L2 Entity (perfil durable) | `customers.attributes` JSONB | Fuente de verdad, no degrada |
| L3 Semantic (RAG de hechos) | pgvector 384 dims | Degrada a "sin recall" |

`EMBEDDING_PROVIDER`: `none` (default — capa apagada, sin sorpresas de dependencias),
`local` (sentence-transformers all-MiniLM-L6-v2, $0, in-process, extra pip opcional),
`openai` (text-embedding-3-small, para escala).

## Justificación

- `none` por defecto evita el error de agent-sale al revés: no prometemos memoria
  vectorial sin proveedor; se activa explícitamente con una decisión informada.
- sentence-transformers local honra la filosofía del monolito: cero APIs externas,
  cero latencia de red, cero costo.
- 384 dims fijas en la migración: cambiar a OpenAI (1536) requiere migración explícita
  (documentado en la columna).

## Consecuencias

- (+) Cada capa degrada con gracia; el asistente nunca se cae por memoria.
- (−) sentence-transformers agrega ~200MB al contenedor si se activa (por eso es
  extra opcional: `pip install 'agent-core[embeddings-local]'`).
