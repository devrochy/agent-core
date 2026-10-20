## Pack de conocimiento, parte 1: modelo y embeddings (HU-AOS-49, ADR-0145)

- **`pack-knowledge` 0.1.0, la habilidad «Conocimiento».** Trae las tablas `pack_knowledge_documents` y
  `pack_knowledge_chunks`.
  - Llevan `tenant_id`, RLS forzada, una FK compuesta y `embedding vector(1536)`.
  - Llevan también `tsv` en español sin tildes (`public.es_unaccent`).
  - Todavía no tiene tools ni subida de documentos: llegan en las partes 2 y 3.
- **Servicio de embeddings del core, `core/llm/embeddings.py`.**
  - `KNOWLEDGE_EMBEDDING_PROVIDER` vale `hash` (por defecto, sin red ni costo) u `openai`
    (`text-embedding-3-small`, que solo funciona con `APP_ENV=production`).
  - La ingesta escribe en `usage_ledger` una fila `kind='embedding'` con créditos en 0: la plataforma los
    absorbe.
- **Lectores del ledger que cuentan turnos:** ignoran las filas `embedding`. Son los costos del panel, la
  data por agente y el primer turno de los primeros pasos.
- **Topes por plan `documentos` / `conocimiento_mb`:**

  | Plan | documentos | conocimiento_mb |
  |---|---|---|
  | Free | 3 | 10 |
  | Basic | 20 | 100 |
  | Premium | 100 | 500 |
  | Empresarial | 500 | 2.000 |

  Los cuenta el pack y los aplica `enforce_pack_growth`, con 409 `plan_limit_<clave>`.
- **`agentcore-contracts` 3.1.0:** `AgentSpecV3.knowledge` es opcional, con `mode`, `topK` (1 a 8) y
  `minScore`. Hay adenda de ADR-0113: un campo opcional nuevo es un minor.
- **Postgres:** imagen fija en `pgvector/pgvector:0.8.7-pg17`. La migración del pack exige pgvector ≥ 0.8.
- **Producción:** al activar el pack de conocimiento en producción, poner `KNOWLEDGE_EMBEDDING_PROVIDER=openai`
  (ya existe `OPENAI_API_KEY`). En test no hace falta nada.
