## Pack de conocimiento, parte 3: búsqueda y tool `buscar_conocimiento` (HU-AOS-49, ADR-0145)

- **Búsqueda en los documentos de cada agente:** híbrida por defecto (vector + texto en español sin
  tildes, fusionados por RRF) o semántica, con `topK` y umbral del bloque `knowledge` del agente.
  - Es exacta y prefiltrada por negocio y agente, así que un negocio grande no le quita resultados a
    uno chico.
- **Tool `buscar_conocimiento`** (pack-knowledge 0.3.0):
  - la ve solo un agente v3 que la declare en sus herramientas y tenga documentos listos;
  - devuelve los fragmentos con documento, sección y páginas para citar;
  - se suma el playbook `conocimiento`.
- **Core:**
  - `ToolContext` suma `agent_id`, `agent_version_id` y `knowledge` (adenda de ADR-0118);
  - `MCPTool.resumen_de_auditoria`: la auditoría guarda solo ids y puntajes de los fragmentos, nunca
    el texto.
- **Evals:**
  - golden set de recuperación de 50 preguntas sobre un PDF, un DOCX y un TXT (`evals/conocimiento.py`);
  - grabación de embeddings para replay (`core/llm/embeddings.grabacion`);
  - el script `scripts/grabar_conocimiento.sh`, que se corre con el OK del dueño (≈ US$0,001).
- **Golden set grabado y calibrado** (`evals/conocimiento/grabacion.json`, `text-embedding-3-small`):
  - híbrida: recall@4 0,95 y MRR 0,90;
  - `minScore` por defecto según el modo: híbrida 0,45, semántica 0,30. En `AgentSpecV3.knowledge`,
    `minScore` pasa a ser opcional;
  - las preguntas sin respuesta quedan como piso de regresión (≥ 5/10). Que el agente no invente se
    mide en su respuesta.
