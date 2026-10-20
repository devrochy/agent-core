# ADR-0145 — Pack de conocimiento: documentos por agente en pgvector con RLS, búsqueda híbrida y embeddings medidos y grabados

- **Estado:** aceptado (HU-AOS-49, SPEC-31, D-38). El dueño lo aprobó el 2026-10-19 con los valores
  recomendados: los límites por plan de §2, los embeddings absorbidos (créditos 0), la URL pospuesta y
  `hash` en el entorno de test. Las decisiones revisables están marcadas *Revisable:*. Siguen abiertas
  dos **Preguntas al dueño**: el OK para grabar el golden set (§3) y qué negocio lo usa primero.
- **Fecha:** 2026-10-19
- **Contexto:** HU-AOS-49 y D-SPEC-31-4 (`spec.md`), inventario ítem 23 y tablas/API de AOS-49 (`design.md`)
  en `solvershub-docs`.
  **Enmienda:**
  - ADR-0113: bloque opcional `knowledge` en AgentSpec v3 y la regla «campo opcional nuevo = minor».
  - ADR-0118: `ToolContext` gana la identidad del agente.
  - ADR-0119: grabaciones de embeddings del conocimiento.
  - ADR-0052: descarga pública solo para la ingesta.
  - ADR-0053: PII en documentos del negocio.
  - ADR-0045/0142: helper genérico de crecimiento.

  **Usa:** ADR-0117 (releases), ADR-0144 (créditos en `usage_ledger`), ADR-0058 (imagen de Postgres),
  ADR-0072/`purge_tenant`.
- **Revisión:** el borrador pasó por `arquitecto` y `auditor-multitenant`. Sus cambios están incorporados.

## Contexto

El dueño pidió, como tester, «un pack de rag» para adjuntar conocimiento a un agente y elegir cómo busca.
No hay nada construido.

`rag_top_k` **no es un ajuste global**: es `AgentSpecV3.memory.rag.{topK,minScore}`, por agente, y solo
alimenta `vector_memory.recall` sobre la **memoria del cliente** (`memory_vectors`).

pgvector (0.8.7 en la imagen `pgvector/pgvector:pg17` actual) y `unaccent` vienen en la imagen de test,
producción y CI. La extensión existe desde la 0001. El cliente de embeddings de OpenAI ya existe y la llave
está en producción.

## Decisión

Se construye **`pack-knowledge`**, una habilidad instalable por scope (ADR-0118), con estas piezas:

- documentos y fragmentos de cada agente en el **mismo Postgres con pgvector**, con `tenant_id` y RLS
  forzada;
- ingesta asíncrona por el worker;
- búsqueda **híbrida** (vector y `tsvector` en español, fusionados con RRF), configurable por agente;
- la tool `buscar_conocimiento`.

Los embeddings son un **servicio del core**: se miden en `usage_ledger` y no hacen llamadas reales fuera de
producción.

### 1. Almacenamiento: pgvector en el mismo Postgres (D-SPEC-31-4 = A)

**Tablas del pack**, con el nombre de la convención (`pack_<x>_*`). `design.md` las llama
`knowledge_documents`/`knowledge_chunks`; ver el seguimiento en docs.

- **`pack_knowledge_documents`**
  - Columnas: `id`, `tenant_id`, `agent_id`, `titulo`, `origen` (`archivo`|`url`), `mime`, `bytes`,
    `sha256`, `url_origen`, `ruta_archivo`, `estado`, `error_codigo`, `error_detalle`, `paginas`,
    `num_fragmentos`, `tokens_embebidos`, `embedding_model`, `embedding_dim`, `chunker_version`,
    `pii_detectada` (conteo), `created_by`, `created_at`, `processed_at` y `deleted_at`.
  - `ruta_archivo` es relativa y la calcula el servidor.
  - Un documento pertenece a **un agente**. *Revisable:* si el dueño quiere compartir un documento entre
    varios agentes, se agrega la tabla de vínculo. Hoy no hay un segundo caso real, y subir el mismo
    archivo a dos agentes cuesta centavos.
- **`pack_knowledge_chunks`**
  - Columnas: `id`, `tenant_id`, `document_id`, `ordinal`, `contenido` (nullable, ver §6), `seccion`
    (encabezado más cercano), `pagina_desde`, `pagina_hasta`, `tokens`, `embedding vector(1536)`
    (nullable), `tsv` y `deleted_at`.
  - `tsv` es `tsvector GENERATED ALWAYS AS (to_tsvector('public.es_unaccent', coalesce(contenido,'')))
    STORED`.

**Integridad entre tenants:**

- Documentos tiene `UNIQUE (tenant_id, id)`. Fragmentos tiene `FOREIGN KEY (tenant_id, document_id)
  REFERENCES pack_knowledge_documents (tenant_id, id)`.
- Un fragmento no puede apuntar al documento de otro negocio: lo impide el esquema, no el código.
- Hacia `agents` (una tabla del core) la FK es **lógica**, como en el resto de los packs (`new-pack`). La
  API resuelve el agente dentro del tenant de la sesión, y un test lo cubre.
- Todo `JOIN` de la búsqueda lleva `tenant_id` en el `ON`.

**Índices:** todo btree empieza por `tenant_id`.

- `(tenant_id, agent_id, estado) where deleted_at is null`.
- `(tenant_id, document_id, ordinal)`.
- `uq (tenant_id, agent_id, sha256) where deleted_at is null`, que impide subir dos veces el mismo
  archivo al mismo agente.
- **GIN** sobre `tsv`.

**Dimensión fija en la migración, `vector(1536)`**, sin atarla a `EMBEDDING_DIMENSIONS`. Ese setting es
de la memoria (384 con MiniLM) y cambiarlo ya obligó a la 0046. El documento guarda `embedding_model`,
`embedding_dim` y `chunker_version`: cambiar de modelo o de troceado es una migración más un re-embebido.
*Revisable:* se usa `halfvec` o `dimensions=512` si las tablas pasan de ~2 GB.

**Búsqueda exacta prefiltrada, sin HNSW al inicio:**

- El índice HNSW no puede encabezarse por `tenant_id`: recorre los vectores de **todos** los negocios y
  la RLS filtra después. Con eso, un negocio chico recibiría menos de `k` resultados según el volumen de
  los demás, incluso con `iterative_scan`, que está acotado por `max_scan_tuples`.
- La consulta prefiltra con un CTE `MATERIALIZED` por el btree del tenant y el agente. Después ordena
  esos fragmentos de forma exacta con `ORDER BY embedding <=> :q LIMIT k`.
- Con los topes por plan (§2), son unos miles de fragmentos por agente: milisegundos.
- HNSW (`vector_cosine_ops`, parcial por `deleted_at is null`, con `SET LOCAL hnsw.iterative_scan`) entra
  solo si se cumple la condición de revisión. Ahí se mide con `EXPLAIN` como `app_runtime`.
- Para mejorar el rendimiento nunca se usan `LEAKPROOF`, `BYPASSRLS` ni el rol dueño.

**RLS:** se activa con `enable` y la policy `tenant_isolation`, con `nullif(current_setting('app.tenant_id',
true), '')::uuid` en `using` y en `with check`, igual que `pack_payments`. `FORCE ROW LEVEL SECURITY` va en
una migración propia del pack que recorre `pack_knowledge_%`.

**Versión de la extensión:**

- La migración del pack exige `extversion >= 0.8` (`ALTER EXTENSION vector UPDATE` como dueño, y si no
  alcanza, falla con un mensaje claro). La extensión se creó en la 0001 y no sube sola al cambiar la
  imagen.
- Se fija el tag de `docker/postgres/Dockerfile` (hoy es el flotante `pgvector/pgvector:pg17`).
- Se corrige la etiqueta `pg16` del compose local.

**Configuración de texto `public.es_unaccent`** (`spanish` + `unaccent`): la crea la migración como dueño y
se nombra calificada. Hace que «envio» encuentre «envío».

### 2. Ingesta: PDF, DOCX y TXT/Markdown, asíncrona por el worker

**Estados del documento:** `pendiente → procesando → listo | fallido`.

- `fallido` lleva un `error_codigo`: `sin_texto` (PDF escaneado; no hay OCR en esta versión),
  `muy_grande`, `formato`, `limite_plan` o `proveedor`.
- El panel permite **reprocesar**: el documento vuelve a `pendiente`.
- Los logs y `error_detalle` llevan códigos, ids y conteos, **nunca** texto del documento.

**Subida:** `POST /api/v1/admin/agents/{agent_id}/knowledge` (multipart).

- Resuelve el agente dentro del tenant de la sesión. Un id de otro negocio responde 404.
- Usa el permiso de la sección `agents` (ADR-0140) en nivel de escritura.
- Valida el MIME contra la allowlist (PDF, DOCX, `text/plain`, `text/markdown`) **y los magic bytes**,
  como `core/qa/evidencias.py`.
- Lee el cuerpo con tope (`read(MAX + 1)`).
- Guarda el archivo, crea la fila en `pendiente`, aplica el límite del plan y encola después del commit.
  Responde 202.
- `GET` lista los documentos con su estado. `DELETE` borra (§6). `POST …/{doc_id}/reprocesar` vuelve a
  procesar.

**Tareas del worker**, encoladas con `TaskQueue.enqueue`:

- Hay dos: `knowledge.extraer(tenant_id, document_id)` y `knowledge.embeber_lote(tenant_id, document_id,
  sha256, chunker_version, desde, hasta)`.
- Cada una abre `tenant_scope(tenant_id)` y lee el documento con la RLS. Si no existe, si el tenant no
  coincide o si la generación (`sha256`, `chunker_version`) cambió, termina sin hacer nada y deja un log.
- Ninguna usa el rol dueño ni funciones SECURITY DEFINER.
- El payload lleva solo ids, nunca texto (el broker es Redis).
- La extracción reemplaza los fragmentos del documento en una transacción. Cada lote embebe 100
  fragmentos con `soft_time_limit` de 60 s.
- El trabajo va en lotes porque el worker tiene **una sola cola** y la comparte con el envío de mensajes.
  *Revisable:* si se atrasa el envío, pasa a una cola `conocimiento` con su propio worker.

**Extracción:**

- PDF con `pypdf` (BSD, sin binarios), DOCX con `python-docx` y TXT/MD con lectura directa.
- Las dependencias van en `packages/pack-knowledge/pyproject.toml`. El `Dockerfile` ya instala todos los
  `packages/pack-*/`, así que no cambia.

**Troceado (`chunker_version = 1`):**

- Primero por estructura: encabezados de DOCX y Markdown, páginas y párrafos del PDF. Después por tamaño.
- Objetivo de **400 tokens**, máximo 600 y **solape de 60** (~15 %), sin partir frases.
- Cada fragmento guarda `seccion` y páginas para la cita.
- Son constantes de la plataforma, no ajustes por agente, porque cambiarlas obliga a re-embeber.
  *Revisable:* se exponen solo si el golden set (§7) muestra que otro tamaño recupera mejor.

**Límites:**

- Por archivo: **20 MB** y **300 páginas**. Por documento: **2.000 fragmentos**.
- Por plan, el **pack cuenta lo suyo** (documentos vivos y MB vivos del tenant) y el core solo aporta:
  - los límites en `plans.py` (`documentos`, `conocimiento_mb`);
  - un helper genérico `enforce_growth(key, actual)` con el advisory lock y el 409 `plan_limit_<clave>`.

  `core/billing/metrics.py` no consulta tablas del pack.

  | Plan | documentos | conocimiento_mb |
  |---|---|---|
  | free | 3 | 10 |
  | basic | 20 | 100 |
  | premium | 100 | 500 |
  | empresarial | 500 | 2.000 |

  **Pregunta al dueño:** los valores de la tabla.

**Archivos:**

- Van en disco, como el resto: no hay blobs en Postgres ni almacenamiento de objetos. La ruta es
  `TENANTS_ROOT/<slug>/conocimiento/<document_id>.<ext>`.
- El servidor pone el nombre. La ruta se resuelve desde la fila y se comprueba que quede dentro del
  directorio del tenant: `../`, rutas absolutas y otro slug se resuelven a nada.
- El original se conserva para reprocesar.

**URL, en una parte aparte y pospuesta (49-4):** ingerir una página web abre una salida a red nueva hacia
cualquier host público. `ScopedHttp` es fail-closed, sin allowlist no sale nada y no sigue redirecciones.

- Si se hace, es una **primitiva del core**, `core/net/descarga_publica.py`, que enmienda ADR-0052:
  - reutiliza el bloqueo de IP y el anclaje del DNS a la IP validada de `ScopedHttp`;
  - lee en streaming con tope de 5 MB y 15 s;
  - acepta como mucho 3 redirecciones y revalida en cada una el esquema, el host, el DNS y la IP;
  - acepta solo `text/html` y `text/plain`.
- Se inyecta en la tarea de ingesta y nunca en `ToolContext`.
- `pack_knowledge` no importa `httpx`.
- **Pregunta al dueño:** ¿se necesita la URL en la primera entrega? La recomendación es no.

### 3. Embeddings: servicio del core, OpenAI `text-embedding-3-small`, medido y grabado

**Dónde vive:** en `core/llm/embeddings.py`. El pack lo importa, tanto la tool como las tareas del
worker, que no tienen `ToolContext`. La memoria lo adopta en un cambio aparte. El setting es del core,
`KNOWLEDGE_EMBEDDING_PROVIDER` (`openai`|`hash`|`grabado`), y lo lee el servicio: el pack nunca lee el
entorno.

**Modelo:** `text-embedding-3-small` (1536 dimensiones, multilingüe, ~US$0,02 por millón de tokens).

- La llave y el cliente ya están en producción. DeepSeek no ofrece embeddings.
- *Revisable:* Voyage `voyage-3.5-lite` cuesta lo mismo, pero es un proveedor y un contrato de datos
  nuevos. Solo se adopta si el golden set en español mejora claramente.
- **Siempre con la llave de la plataforma** (`key_mode = platform`), también para negocios BYOK.
  *Revisable* si un negocio BYOK lo pide.

**Costo estimado:**

| Caso | Tokens embebidos | Costo |
|---|---|---|
| FAQ de 10 páginas | ~6.000 (con solape) | ≈ US$0,00012 (≈ COP 0,5) |
| Catálogo de 50 páginas | ~35.000 | ≈ US$0,0007 (≈ COP 3) |
| Negocio con 100 documentos medianos | ~1,5 M | ≈ US$0,03, una vez |
| Una consulta | ~20 | ≈ US$0,0000004 |

**El embedding es despreciable.** El costo real son los fragmentos inyectados en el turno: `topK × ~400
tokens`, es decir 1.600 tokens de entrada con `topK = 4`. Ya los mide la fila `kind='turn'` y los cobra la
fórmula de ADR-0144. Por eso `topK` tiene tope (§4).

**Ledger:**

- **Una fila por lote de ingesta**, con `kind='embedding'`. El check de la 0070 ya lo permite y hoy no se
  usa.
  - Lleva `model`, `input_tokens`, `cost_usd` real (precio en `PRICING`) y `key_mode`.
  - `conversation_id`, `turn_id` y `agent_instance_id` van nulos.
  - `result = {"proposito": "ingesta", "document_id": …}`.
  - La escribe el servicio dentro del `tenant_scope` de la tarea, con `tenant_id =
    get_current_tenant_id()`, nunca de un argumento.
- **Sin fila por consulta.** Costaría ~US$0,0000004 y quedaría en 0 con `Numeric(10,6)`; además, una fila
  por búsqueda es ruido. Los tokens de la consulta quedan en la auditoría del turno (§4).
- **Créditos:** el tarifario `v1` no tiene concepto `embedding`. La fila se escribe con `credits = 0` y la
  plataforma absorbe el costo, que queda visible en `cost_usd`. Cobrarlo sería publicar el tarifario `v2`
  (ADR-0144 §2). **Pregunta al dueño:** ¿absorber o cobrar? La recomendación es absorber; el control es el
  tope por plan.
- **Lectores que asumen «una fila = un turno».** Hoy solo se escriben `turn` y `eval`. En la 49-1 pasan a
  filtrar `kind = 'turn'` estos lectores:
  - `core/api/routers/costs.py` (`total_turns`);
  - `core/api/routers/conversations.py`;
  - `core/agents/data_por_agente.py`;
  - `core/admin/views.py`;
  - `core/primeros_pasos/calculo.py`. Sin el filtro, subir un documento marcaría «primer turno
    respondido».

  `medir_creditos` ya trata `kind` por su cuenta (ADR-0144 §7).

**Nada de llamadas reales fuera de producción:**

- **Develop, CI y el entorno de test** usan `hash`: un vector determinista que sale del sha256 de los
  n-gramas normalizados.
  - Con `hash`, la parte semántica no significa nada, pero la léxica de la búsqueda híbrida sigue
    encontrando, así que test sirve para probar a mano.
  - **Pregunta al dueño:** ¿embeddings reales en test, con un tope de centavos?
- **Evals:** `evals/grabaciones.py` **mantiene** `EMBEDDING_PROVIDER=none` para la memoria, porque el
  recall cambiaría el prompt y rompería las grabaciones actuales. Solo el conocimiento pasa a `grabado`.
  - Las grabaciones de embeddings se guardan junto a las del LLM, con su propia clave
    `sha256(modelo, dimensión, texto)`, sin el normalizador de `core/llm/cassette.py`, que reemplaza
    datos por marcadores y haría colisionar textos distintos.
  - En replay, un texto sin grabar falla con `GrabacionDesactualizada` y nunca sale a la red (ADR-0119).
  - Las grabaciones con datos de un negocio real van en `TENANTS_ROOT/<slug>/evals/cassettes`, nunca en el
    repo.
  - No hay caché en memoria de embeddings por texto en el runtime.
- **Grabar el golden set** (§7) es una llamada real única, de ~50.000 tokens (≈ US$0,001). La hace el
  dueño, o se hace con su OK.

### 4. Búsqueda: híbrida por defecto, configurable por agente

**Modos:** `semantica` (coseno) e `hibrida`, que es el modo por defecto.

- La HU pide poder elegir («elijo búsqueda semántica o híbrida»), así que el selector se queda.
- La híbrida va por defecto por tres razones:
  - las preguntas frecuentes y los catálogos traen referencias, nombres propios y precios que la búsqueda
    semántica sola falla;
  - con `hash` en test, la búsqueda semántica sola no encontraría nada;
  - cuesta lo mismo.
- *Revisable:* si el golden set muestra que la semántica nunca gana, el selector se esconde en el panel
  (el campo del contrato sigue existiendo).

**Híbrida:** toma los 20 primeros por vector y los 20 primeros por `ts_rank_cd(tsv,
websearch_to_tsquery('public.es_unaccent', q))`, y los fusiona por **RRF** (`1/(60 + rango)`). Todo va en
una sola consulta sobre el CTE prefiltrado (§1), dentro del `tenant_scope` y con `tenant_id` explícito:
agente, documento `listo` y `deleted_at is null`.

**Umbral:** `minScore` se aplica a la similitud coseno. Un fragmento que entra solo por la parte léxica pasa
si su `ts_rank` supera un piso fijo. Así, una pregunta que no está en los documentos devuelve **cero
fragmentos**, y el agente dice que no sabe o escala: no inventa.

**Configuración por agente:** un bloque opcional de primer nivel en AgentSpec v3:

```text
knowledge = {mode: "semantica"|"hibrida" = "hibrida", topK: 1..8 = 4, minScore: 0..1 = 0.35}
```

- **No va en `memory.rag`.** Ese bloque es el recall de la memoria del cliente: si se mezclaran, bajar el
  `topK` de documentos cambiaría lo que el agente recuerda de la persona. Tampoco va dentro de `memory`,
  porque el conocimiento no es memoria. El panel los nombra «Memoria del cliente» y «Conocimiento».
- **Enmienda de ADR-0113, regla de versión:** un campo **opcional con default** en un modelo existente es
  **minor**.
  - `agentcore-contracts` pasa de 3.0.0 a **3.1.0**. Quitar o cambiar un campo sigue siendo major.
  - El lector tolerante y el editor podan `knowledge` si la habilidad no está en la release.
  - *Consecuencia aceptada:* cuando exista una `agent_version` guardada con `knowledge`, el binario no
    puede volver por debajo de 3.1.0, porque `extra="forbid"` la rechazaría. El orden de despliegue es
    primero el contrato y después el editor.
- Queda versionado con el agente, así que entra en las releases y el canary (ADR-0117). El tope de `topK`
  en 8 acota el gasto por turno.
- **Los documentos no van en la release.** Son contenido, como el catálogo de Comercio: subir o borrar
  uno cambia la respuesta del agente al instante, sin canary. Se acepta. *Revisable:* si un documento
  malo causa un incidente, se agrega un estado «en revisión» antes de `listo`.

**Cita de la fuente:** la tool devuelve una lista tipada de fragmentos con `{documento, seccion, paginas,
texto, score}`. El playbook del pack indica responder con esa información y, si el cliente pregunta de
dónde sale, nombrar el documento y la sección. Al cliente nunca se le muestran ids ni scores.

**Auditoría del turno:** `tool_calls` guarda `document_id`, `chunk_id`, `score` y los tokens de la
consulta, **no el texto**. Así, borrar un documento borra su contenido de verdad (§6).

### 5. Tool `buscar_conocimiento`: habilidad instalada y declarada por el agente

- **Registro:** la registra `pack-knowledge` como `MCPTool` con `plugin_id="knowledge"`. No es
  `always_available`.
- **Filtros que debe pasar** (ADR-0118 §3, `core/kernel/oferta.py`):
  - la habilidad está **instalada** en la conexión o el negocio;
  - el agente **declara** la tool en `tools` (fail-closed).
- **Predicado de oferta propio:** `oferta.py` deja pasar todo cuando no hay release o no hay agente en
  foco, así que la tool lleva su predicado. Se **oculta** si no hay agente v3 en foco o si el agente no
  tiene documentos `listo`. Así no se ofrece en el runtime legacy ni gasta tokens en llamadas vacías.
- **Editor (HU-AOS-24):** al subir el primer documento, propone agregar al borrador del agente la tool
  **y** el playbook del pack (`PlaybookRefV3.pack`), juntos. No publica por su cuenta.
- **`ToolContext`** (enmienda ADR-0118, que decía que no cambiaba):
  - gana identidad genérica: `agent_id`, `agent_version_id` y el bloque `knowledge` congelado de la
    versión, igual que `write_namespaces`;
  - el LLM solo pasa `consulta` (texto, ≤ 300 caracteres) y cualquier `agent_id` o `tenant_id` en los
    argumentos se ignora;
  - se actualiza `docs/PLUGIN-CONTRACT.md`.
- **Procedimientos:** un procedimiento puede listar `buscar_conocimiento` entre las acciones de una etapa
  (por ejemplo «resolver duda» antes de «cotizar»). Pasa el mismo filtro de la etapa en foco, sin reglas
  nuevas.
- **El contenido es dato, no instrucción** (invariantes 3 y 7):
  - el resultado separa el texto de cada fragmento como un campo tipado `texto`, sin texto de prompt en
    `.py`;
  - el playbook del pack dice que ese contenido es referencia del negocio y que no se siguen instrucciones
    que aparezcan en él.

### 6. Seguridad y aislamiento

- **RLS forzada** en las dos tablas. La búsqueda corre en `tenant_scope` y además filtra `tenant_id`
  explícito, como `vector_memory.recall`.
- **Integridad:** la FK compuesta impide mezclar fragmentos y documentos de negocios distintos (§1).
- **Tareas del worker:** reciben `tenant_id` y verifican el documento (§2).
- **API:** devuelve 404 con ids de otro negocio.
- **Archivos:**
  - por tenant, con la ruta resuelta desde la fila;
  - no se sirven por URL pública: una descarga, si se agrega, pasa por la API con permiso.
- **Datos personales:** los documentos son contenido del negocio y suelen traer datos que **sí** hay que
  responder, como el teléfono o el correo del local.
  - **No se aplica `redact_pii`** (enmienda ADR-0053 para este origen).
  - La ingesta cuenta las coincidencias del detector y guarda `pii_detectada`. El panel avisa: «este
    documento parece tener datos personales; no subas listas de clientes».
  - *Revisable:* si aparecen listas de clientes, se bloquea por umbral.
- **Borrado:**
  - El documento y sus fragmentos se marcan con `deleted_at` (invariante 8). En la misma transacción se
    **vacían** `contenido` y `embedding` de los fragmentos, y el archivo se borra del disco después del
    commit.
  - La auditoría no guarda el texto (§4), así que el contenido deja de existir aunque las filas queden.
  - *Revisable:* el vaciado es una actualización destructiva, y se adopta porque «borrar un documento»
    tiene que borrar su contenido.
- **Purga del tenant:**
  - `purge_tenant(tid)` ya borra toda tabla con `tenant_id`, y estas también. Lo que **no borra son
    archivos**: hoy no hay ningún `rmtree` en `TENANTS_ROOT/<slug>/`.
  - El core cierra el hueco para todos (docs, evidencias y conocimiento):
    - `core/tenants.py` captura el slug **antes** de la purga y hace `rmtree(TENANTS_ROOT/<slug>)`
      después del commit;
    - si falla, deja el log `tenant_purge_files_cleanup_failed` con el UUID y una alerta, con reintento
      por CLI como el de Redis;
    - la alta de un negocio rechaza un slug cuyo directorio exista y no esté vacío, así un slug
      reutilizado nunca hereda archivos de un negocio purgado.

### 7. Evals: golden set de recuperación y criterio de aceptación

**Fixture:** un negocio de ejemplo con 3 documentos: una FAQ en PDF, una política de cambios en DOCX y un
catálogo en TXT.

- **40 preguntas** en español, con variantes sin tildes, sinónimos y referencias. Cada una indica el
  documento y la sección esperados.
- **10 preguntas** que no están en los documentos.

**Métricas**, con embeddings grabados y en replay en develop y CI:

| Métrica | Criterio |
|---|---|
| `recall@4` híbrida | ≥ 0,90 |
| MRR | ≥ 0,70 |
| Preguntas sin respuesta, con cero fragmentos sobre el umbral | ≥ 8 de 10 |
| `recall@4` híbrida frente a la semántica | no queda por debajo |

**Punta a punta:** 5 conversaciones con la grabación del LLM. La respuesta trae el dato del documento y no
inventa. Usa las aserciones de estado de ADR-0119.

**Tests de aislamiento** (integración, no eval), exigidos por parte:

- **49-1**
  - `relrowsecurity` y `relforcerowsecurity` activos, con la policy en `USING` y en `WITH CHECK`.
  - Como `app_runtime` sin scope: cero filas, y el INSERT falla.
  - La FK compuesta rechaza mezclar tenants.
  - Los btree empiezan por `tenant_id`.
  - La fila del ledger toma el tenant del scope.
- **49-2**
  - Tarea con el `tenant_id` de A y el documento de B: no hace nada.
  - Tarea sin `tenant_id`: falla.
  - Rutas manipuladas: no resuelven.
  - API con ids de otro negocio: 404.
  - La purga borra la carpeta, y cuando falla queda la alerta.
  - Un slug reutilizado con residuos se rechaza.
  - El límite cuenta solo los documentos del tenant.
- **49-3**
  - Dos negocios con **el mismo texto**: cero fragmentos cruzados, en los dos modos.
  - Volumen desigual (B con 50.000 fragmentos y A con 10): A sigue recibiendo `k`. El `EXPLAIN` como
    `app_runtime` muestra el prefiltro.
  - Los documentos de otro agente del mismo negocio, los borrados y los que no están `listo` no aparecen.
  - La tool ignora `agent_id` y `tenant_id` en los argumentos.
  - Test estático: `pack_knowledge` no importa `httpx` ni lee el entorno o `get_settings`.
- **49-4**
  - Se rechazan IP privada, loopback, CGNAT, metadata e IPv6 mapeada.
  - Se rechazan la redirección a un host interno, el DNS rebinding, más de 3 redirecciones, más de 5 MB y
    tipos de contenido fuera de la lista.
  - La tool no tiene acceso a la primitiva.

Los umbrales se fijan con la primera grabación. Si no se alcanzan, se ajustan el troceado o el `minScore`,
no los umbrales.

### 8. Plan por partes (cada una ≤ L)

| Parte | Contenido | Tamaño |
|---|---|---|
| **49-1 · Modelo + embeddings** | `pack-knowledge` (manifiesto y migraciones: tablas, FK compuesta, RLS, FORCE, GIN, `es_unaccent`, verificación de `extversion`); tag de la imagen fijo; `core/llm/embeddings.py` con `hash` y `openai`, precio en `PRICING` y fila `embedding`; lectores del ledger filtrando `kind='turn'`; `knowledge` en el contrato 3.1.0 con adenda de ADR-0113, lector tolerante y poda; helper `enforce_growth` genérico y límites en `plans.py` | L |
| **49-2 · Ingesta de archivos** | API subir, listar, borrar y reprocesar, más `openapi.json`; magic bytes; tareas `extraer` y `embeber_lote`; PDF/DOCX/TXT; troceado v1; estados; aviso de PII; límites del pack; purga de archivos y rechazo del slug con residuos en el core | L |
| **49-3 · Búsqueda + tool + evals** | Consulta exacta prefiltrada, semántica e híbrida RRF; `buscar_conocimiento` con su predicado; `ToolContext` con identidad del agente (enmienda ADR-0118, `PLUGIN-CONTRACT.md`); playbook; grabaciones de embeddings del conocimiento; golden set y su grabación con OK del dueño; tests de aislamiento | L |
| **49-4 · URL (pospuesta)** | `core/net/descarga_publica.py` (enmienda ADR-0052), extracción de HTML y sus tests de SSRF. Solo si el dueño la pide | M |
| **49-5 · Panel** (repo `solvershub-panel`) | Pestaña «Conocimiento» en la ficha del agente: subir, lista con estado, borrar, reprocesar y la configuración de búsqueda (modo, fragmentos, umbral) | M |

Dependencias:
- La 49-2 y la 49-3 dependen de la 49-1.
- La 49-5 puede empezar con la API de la 49-2.
- Los Gherkin de la HU se cierran con la 49-3 y la 49-5 desplegadas en test.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Servicio de vectores aparte (Qdrant, Pinecone) | Infraestructura y proveedor nuevos con datos del cliente; el aislamiento se reimplementa fuera de la RLS; otro backup. A esta escala pgvector sobra |
| `memory_vectors` con `kind='document'` (el check ya lo admite) | Dimensión atada a `EMBEDDING_DIMENSIONS` (384); sin `tsvector`, documento, página ni estado; retención y purga pensadas para la memoria del cliente; dos ciclos de vida en una tabla |
| Conocimiento en el core en vez de un pack | Se instala y se cobra como habilidad (ADR-0118), así que quien no lo usa no lo ve. Lo compartido de verdad, los embeddings, sí va al core |
| HNSW desde el inicio | El post-filtro de la RLS hace que un negocio chico reciba menos de `k` según el volumen de otros; comparte RAM con la base operativa. Con los topes por plan, la búsqueda exacta prefiltrada es más rápida y correcta |
| IVFFlat | Necesita entrenar las listas y se degrada al crecer |
| Solo búsqueda semántica | Falla con referencias, nombres y precios; en test, con `hash`, no encontraría nada |
| Configuración en `memory.rag` o dentro de `memory` | Mezcla documentos con la memoria del cliente |
| Configuración en una tabla del pack | No quedaría en las releases ni en el canary |
| Métricas `documentos`/`conocimiento_mb` en `core/billing/metrics.py` | Acopla el core a tablas de packs; con N packs, `metrics.py` se vuelve un switch |
| Fila del ledger por cada consulta | Costo que se redondea a 0 y ruido; rompe a los lectores que cuentan turnos |
| Embeddings locales (sentence-transformers, multilingual-e5) | ~500 MB y CPU en el worker para ahorrar centavos |
| `redact_pii` en los fragmentos | Borraría el teléfono y el correo del negocio, que es justo lo que se pregunta |
| Borrado físico de fragmentos | Contradice la invariante 8; vaciar el contenido logra lo mismo |
| URL desde la primera entrega | Abre la primera salida a red hacia cualquier host; ningún cliente la pidió todavía |

## Consecuencias

**Positivas:**
- El aislamiento usa la misma RLS que el resto, con integridad por FK compuesta, un backup y un motor.
- La ingesta cuesta muy poco y queda medida.
- Develop y CI no gastan nada: usan `hash` y grabaciones.
- La configuración por agente queda versionada en las releases.
- La tool se ofrece solo donde sirve.
- Cuando el dato no está en los documentos, el agente deja de inventar.
- La purga del tenant pasa a borrar también sus archivos.

**Negativas / lo que aceptamos pagar:**
- Cambiar el modelo obliga a re-embeber todo.
- Un PDF escaneado falla, porque no hay OCR.
- La ingesta comparte la cola del worker con los envíos.
- No se puede volver a un binario anterior a contracts 3.1.0 cuando ya existan versiones con `knowledge`.
- Los documentos cambian la conducta del agente sin pasar por el canary.
- Los embeddings no se cobran en créditos hasta el tarifario `v2`.
- Se suman 5 lectores del ledger a corregir.

**Qué haría que revisáramos esto:**
- Un agente pasa de **50.000 fragmentos** o la búsqueda pasa de **150 ms p95**: se mide HNSW con
  `iterative_scan`, y después `halfvec` o una base aparte.
- La cola de envío acumula más de **30 s** de atraso mientras se procesan documentos: se crea una cola
  `conocimiento` propia.
- El golden set no llega a **`recall@4` ≥ 0,90** con OpenAI: se evalúa Voyage.
- Más del **10 %** de los documentos de producción fallan con `sin_texto`: se agrega OCR.

## Impacto en el código

- **Nuevo, `packages/pack-knowledge/`:**
  - `pyproject.toml` con `pypdf` y `python-docx`;
  - las migraciones `pack_knowledge_0001..0003`;
  - `pack_knowledge/{module,models,ingesta,troceado,busqueda,skills,jobs}.py`;
  - el playbook `SKILL.md`;
  - su entrada en `packages/pack-versions.lock.json`.
- **Core:**
  - `core/llm/embeddings.py` y `KNOWLEDGE_EMBEDDING_PROVIDER` en `core/config.py`;
  - `PRICING`;
  - `core/mcp/types.py` (`ToolContext`), y el loop y el motor, que lo llenan;
  - `core/billing/{plans,enforcement}.py`;
  - los lectores del ledger de §3;
  - `core/tenants.py` y `core/alta_negocio.py` (archivos en la purga y en la alta);
  - `evals/grabaciones.py`;
  - `docker/postgres/Dockerfile` (tag fijo) y `docker-compose.yml` (etiqueta);
  - `docs/PLUGIN-CONTRACT.md`.
- **Contratos:** `packages/contracts/agentcore_contracts/agentspec.py` gana `KnowledgeConfigV3` y
  `AgentSpecV3.knowledge`, en la versión 3.1.0. También cambian el lector tolerante, el editor y la adenda
  de ADR-0113.
- **API y panel:** `/api/v1/admin/agents/{id}/knowledge` en `docs/openapi.json` y la pestaña en
  `solvershub-panel`.
- **Docs (seguimiento en `solvershub-docs`):**
  - `design.md` de SPEC-31 pasa a `pack_knowledge_*`;
  - el inventario se corrige: `rag_top_k` es por agente y de la memoria, no un ajuste global;
  - las variables de producción van al catálogo de pruebas.

## Adenda de implementación (2026-10-20, HU-AOS-49 parte 1: modelo y embeddings)

Lo que quedó en código y en qué se aparta del texto de arriba.

- **Pack `pack-knowledge` 0.1.0**, con `plugin_id = "pack_knowledge"` (la convención de los ids de los
  packs, no `knowledge`).
  - Todavía no registra tools ni rutas, así que instalado no cambia lo que hace ningún agente.
  - Aparece en el catálogo de habilidades como «Conocimiento», con la guía «en preparación».
  - Su versión está en `packages/pack-versions.lock.json`.
- **Migraciones del pack:**
  - una sola, `pack_knowledge_0001`: tablas, FK compuesta, checks, índices btree que empiezan por
    `tenant_id`, GIN parcial sobre `tsv`, RLS habilitada y forzada con la policy, `unaccent` y
    `public.es_unaccent`, y la exigencia de pgvector ≥ 0.8;
  - FORCE va en la misma migración (lo exige el hook `migraciones-rls`), no en una 0002 aparte como en
    los packs viejos.
  - No hay índice HNSW (§1).
  - Probadas sobre la imagen del CI (`solvershub-postgres:pg17-postgis`, pgvector 0.8.7).
- **Imagen:** `docker/postgres/Dockerfile` queda fijo en `pgvector/pgvector:0.8.7-pg17`. El compose local
  pasa a etiquetar la imagen `pg17-pgvector-postgis`.
- **Servicio `core/llm/embeddings.py`:**
  - `embeber_conocimiento(textos, proposito, document_id)` con los proveedores `hash` (por defecto) y
    `openai`.
  - `openai` solo funciona con `APP_ENV=production`. Fuera de producción se usa `hash` con un aviso.
  - El entorno de test no necesita variable: el valor por defecto es `hash`, como decidió el dueño. En
    producción va `KNOWLEDGE_EMBEDDING_PROVIDER=openai`.
  - El proveedor `grabado` de las evals llega con la parte 3.
- **Precio:** va en `PRECIOS_EMBEDDINGS` (US$0,02 por millón de tokens), **no en `PRICING`**. El editor
  arma el catálogo de modelos de un agente desde `PRICING`, y un modelo de embeddings no es elegible.
- **Fila del ledger de la ingesta:**
  - `kind='embedding'`, con `credits = 0` y `key_mode = platform`;
  - además `tarifario_version` y `trm_cop`, porque `ck_usage_credits_completos` (ADR-0144) los exige
    cuando hay `credits`;
  - `turn_id` y `conversation_id` van en NULL;
  - `result = {proposito, document_id}`, sin texto.
- **Lectores del ledger:**
  - filtran **`kind != 'embedding'`**, no `kind = 'turn'`, para no cambiar lo que hoy cuentan las filas
    `eval` o `agent_run`;
  - son `costs.py` y `views.py` (total de turnos y costo por conversación), `data_por_agente.py`
    (actividad y consumo por agente) y `primeros_pasos/calculo.py` (primer turno);
  - las sumas de costo del negocio (`costo_llm_usd`, el tablero) **sí** incluyen los embeddings, porque
    son gasto de LLM.
- **Topes:**
  - `PACK_LIMIT_KEYS = {documentos, conocimiento_mb}` en `core/billing/plans.py`, con los valores de
    §2. No son métricas del registro, así que no salen en las barras de consumo de `/admin/plan`. Sí
    salen en la comparación de planes (`plans[].limits`).
  - El core suma `enforcement.enforce_pack_growth(tenant, key, contar, incremento, session)`: toma el
    advisory lock antes de contar y responde 409 `plan_limit_<clave>`.
  - El pack cuenta lo suyo en `pack_knowledge/limites.py` (`exigir_cupo`). La subida de la parte 2 lo
    usa.
- **Contrato:** `agentcore-contracts` 3.1.0, con la adenda de ADR-0113 del 2026-10-20.
  - **No se poda** `knowledge` cuando la habilidad no está en la release: el bloque es inerte sin la tool
    declarada.
  - El texto de §4 («el lector tolerante y el editor podan») queda así reemplazado. Podarlo borraría la
    configuración de un agente al desinstalar y reinstalar la habilidad.
- **Revisión del `auditor-multitenant` (sin hallazgos altos), incorporada:**
  - `app_runtime` tiene solo `SELECT`, `INSERT` y `UPDATE` en las dos tablas: `revoke all` más
    `grant` explícito, sin `DELETE` (invariante 8);
  - `enforce_pack_growth` rechaza un `tenant_id` distinto del `tenant_scope` vigente;
  - `exigir_cupo` toma el tenant del scope;
  - hay tests del `WITH CHECK` con scope activo (insertar y mover a otro negocio) y de los permisos.
- **Tests:**
  - unitarios: `tests/test_embeddings_conocimiento.py` y `tests/test_pack_knowledge_limites_y_contrato.py`;
  - de integración, con la marca `aislamiento`: `tests/integration/test_pack_knowledge_aislamiento.py`.
    Cubre RLS forzada y la policy, que otro negocio o una sesión sin negocio vean 0 filas, la FK
    compuesta, los índices por `tenant_id`, `tsv` sin tildes, el orden por coseno y la fila del ledger
    con el tenant del scope.

## Adenda de implementación (2026-10-20, HU-AOS-49 parte 2: ingesta de archivos)

- **`pack-knowledge` 0.2.0.** Agrega `pypdf` y `python-docx` como dependencias del pack. La imagen ya
  instala todos los `packages/pack-*`. Ninguna de las dos usa binarios del sistema.
- **API en `/api/v1/pack/pack_knowledge/agents/{agent_id}/documents`.** Es el prefijo obligatorio de las
  rutas admin de un pack (ADR-0079); reemplaza el `/api/v1/admin/agents/{id}/knowledge` de §2.
  - Operaciones: `GET` (lista), `POST` multipart (sube, responde 202), `DELETE …/{document_id}` (204) y
    `POST …/{document_id}/reprocess` (202).
  - Permisos: sección `agents`, con escritura para subir, borrar y reprocesar.
  - Solo admite especialistas vivos del negocio de la sesión. Otro negocio, el Coordinador o el
    documento de otro agente responden 404, sin eco del id.
  - Errores: 413 `muy_grande`, 422 `formato`/`vacio`, 409 `documento_duplicado` (el mismo `sha256` en el
    mismo agente) y 409 `plan_limit_documentos`/`plan_limit_conocimiento_mb`, con sus mensajes en
    `core/api/plan_limits.py`.
  - Las rutas se montan con el pack detectado, como las de los otros packs. No exigen que la habilidad
    esté instalada en una conexión: el documento sirve recién cuando el agente declare la tool (parte 3).
- **Tipo y contenido** (`archivos.py`):
  - el tipo sale del MIME declarado o, si es genérico (`application/octet-stream`, `text/*`), de la
    extensión: los navegadores mandan `.md` como binario;
  - el contenido lo confirma: `%PDF-`, un ZIP con `word/document.xml`, o texto sin NUL y en UTF-8 (con
    respaldo latin-1).
- **Tareas** (`register_task` es nuevo en `core/tasks/decorators.py`, paralelo a `register_cron`):
  - `pack_knowledge.extraer`: extrae, trocea y reemplaza los fragmentos;
  - `pack_knowledge.embeber`: un lote de 100. Se reencola mientras queden fragmentos sin vector, así
    los lotes van **en cadena, nunca en paralelo**.
  - No hay `soft_time_limit`: Celery lo lanza en el hilo y no en el loop async de `run_async`. Cada
    lote es una llamada a la API (segundos) y queda bajo el `task_time_limit=300` global.
  - La extracción corre en `asyncio.to_thread`.
  - Cada tarea recibe `tenant_id` y `document_id`, abre `tenant_scope` y lee con un filtro explícito. Un
    documento que no es de ese negocio, que está borrado o que está en otro estado no hace nada.
  - El documento se bloquea (`FOR UPDATE`) al escribir.
  - Un error se registra sin reintento y deja el documento `fallido`: el dueño lo reprocesa.
- **Reprocesar** se permite en cualquier estado: vuelve a `pendiente` y se encola la extracción, que
  vacía los fragmentos anteriores.
- **Borrar:** `deleted_at` en el documento y los fragmentos, con `contenido` y `embedding` en NULL en la
  misma transacción, y el original se borra del disco después del commit.
- **PII:** `core.memory.vector.contar_pii` usa los mismos patrones de `redact_pii`, pero no enmascara.
  Guarda el conteo en `pii_detectada`.
- **Purga del tenant:**
  - `core/tenants.py` borra `TENANTS_ROOT/<slug>/` después del commit, con el slug capturado antes.
    Si falla, deja el log `tenant_purge_files_cleanup_failed`, manda una alerta crítica y la función
    devuelve False.
  - El alta de un negocio **nuevo** manda a `TENANTS_ROOT/.purgados/` las subcarpetas con datos de
    clientes que encuentre con ese slug (`conocimiento/`, `evidencias/`), y no rechaza el alta.
  - `docs/` se queda: se puede preparar antes del alta (voz de marca), y moverla rompería una base de
    desarrollo recreada.
- **Revisión del `auditor-multitenant` (sin hallazgos altos), incorporada:**
  - **la purga renombra antes de borrar:** `borrar_carpeta_del_tenant` mueve `TENANTS_ROOT/<slug>` a
    `.purgados/<slug>-purgado-<sello>`, que es atómico, y después hace `rmtree`. Así el slug queda libre
    aunque el borrado falle. Si el renombre falla, se manda la alerta;
  - el alta de los datos de prueba del tester (`core/qa/datos.py`, que crea la fila del negocio antes
    del alta) también pasa por la cuarentena;
  - las excepciones inesperadas del worker dejan el documento `fallido` con el código nuevo **`interno`**
    (migración `pack_knowledge_0002`) y la clase en `error_detalle`;
  - `register_task` registra solo la clase del error: un error de SQLAlchemy trae los parámetros, que
    pueden ser texto del documento;
  - `archivos.carpeta` usa el slug validado (`onboarding.carpeta_del_tenant`).
  - **Pendiente:** retención de `.purgados/` (hoy queda hasta que alguien la vacíe) y un comando
    `agent-core tenant purge-files <slug>`. Hasta que existan, la alerta dice que se borre a mano.
- **Tests:**
  - unitarios en `tests/test_pack_knowledge_ingesta_unit.py`: tipo y contenido, rutas sin traversal,
    extracción de PDF, DOCX, TXT y MD, troceado, `contar_pii` y tareas registradas;
  - de integración en `tests/integration/test_pack_knowledge_ingesta.py` (`aislamiento`): subir →
    `listo` → listar → borrar, el PDF escaneado como `fallido/sin_texto` con reprocesar, los tipos
    falsos, el 404 desde otro negocio en todo, el otro agente y el Coordinador del mismo negocio, la
    tarea con un tenant ajeno, el payload que lleva solo ids, el tope que cuenta solo el negocio, y la
    purga con su cuarentena;
  - unitarios de la tarea sin `tenant_id`, del log de errores sin parámetros, de la purga con el disco
    fallando (alerta y False), del renombre y de la cuarentena.

## Adenda de implementación (2026-10-20, HU-AOS-49 parte 3: búsqueda, tool y golden set)

- **Búsqueda** (`pack_knowledge/busqueda.py`):
  - un CTE `MATERIALIZED` con los fragmentos vivos de los documentos `listo` del agente, con
    `tenant_id` explícito en el `WHERE` y en el `JOIN`, más la RLS. Sobre ellos calcula la similitud
    coseno exacta y `ts_rank_cd`;
  - `semantica`: similitud ≥ `minScore`;
  - `hibrida`: RRF de los 20 primeros de cada lista. Un fragmento entra con similitud ≥ `minScore` o
    con rango léxico ≥ `PISO_LEXICO`;
  - la consulta léxica une los términos con **OR** (`plainto_tsquery` con `&` → `|`): con AND, casi
    ninguna pregunta en lenguaje natural encuentra un fragmento.
- **Configuración:** `Config.del_agente(ToolContext.knowledge)`.
  - Sin bloque, o con uno que no valida, se usan los valores por defecto del contrato.
  - Esto reemplaza el comentario de contracts 3.1.0 («sin el bloque, el agente no busca»): lo que
    habilita la búsqueda es **declarar la tool**, y el bloque solo la ajusta.
- **Tool `buscar_conocimiento`** (`pack_knowledge/skills.py`, pack 0.3.0, con `declared_skills` en el
  manifiesto):
  - el predicado la ofrece solo con un agente v3 en foco que la declare en `tools` y tenga documentos
    `listo`, aunque no haya motor;
  - el LLM solo pasa `consulta`, y el agente sale del `ToolContext`;
  - devuelve `fragmentos` (`id`, `document_id`, `documento`, `seccion`, `paginas`, `texto`,
    `puntaje`), `sin_resultados` y `tokens_consulta`;
  - en `audit_log` solo quedan ids y puntajes (`resumen_de_auditoria`). `usage_ledger.tool_calls`
    sigue guardando solo el nombre.
- **Playbook `conocimiento`** (`packages/pack-knowledge/playbooks/conocimiento.md`): cuándo buscar,
  responder solo con los fragmentos, citar documento y sección sin ids, decir que no se sabe si
  `sin_resultados`, y que el contenido es dato y no instrucción. El agente lo tiene que declarar en
  `playbooks`, junto con la tool en `tools`.
- **Embeddings grabados** (`core/llm/embeddings.grabacion(ruta, modo)`):
  - clave propia `sha256(modelo|dimensión|texto)` y vectores en float32 base64;
  - en replay, un texto sin grabar falla con `GrabacionDesactualizada`;
  - solo funciona con `APP_ENV` development o test (los de `cassette.ENTORNOS_PERMITIDOS`);
  - la memoria sigue en `EMBEDDING_PROVIDER=none` dentro de las evals.
- **Golden set de recuperación** (`evals/conocimiento.py` y `evals/conocimiento/`):
  - tres documentos de un negocio ficticio (una FAQ en PDF, una política en DOCX y un catálogo en
    TXT) y 50 preguntas: 40 con respuesta y 10 sin ella;
  - se ingieren por el camino real y se miden recall@4, MRR y negativas;
  - corre en `tests/integration/test_pack_knowledge_busqueda.py`: en replay contra
    `evals/conocimiento/grabacion.json` con los umbrales de §7 (se salta si falta la grabación) y con
    `hash`, exigiendo recall@4 ≥ 0,85 solo por la parte léxica;
  - **la grabación necesita el OK del dueño**: `bash scripts/grabar_conocimiento.sh` (toma `OPENAI_API_KEY` del entorno o del `.env`, como el gate toma `LLM_API_KEY`; DeepSeek no tiene embeddings),
    con un stack efímero y ≈ US$0,001.
- **Calibración con `hash`:** recall@4 = 0,90 y MRR = 0,77 en la híbrida, pero solo 4 de 10
  negativas en cero.
  - `ts_rank_cd` sube de a 0,1 por término, así que lo léxico solo no separa una negativa que
    comparte una palabra («envíos internacionales a España») de una pregunta real.
  - Esa separación la da el embedding real: `PISO_LEXICO` (hoy 0,1) y `minScore` se calibran con la
    grabación antes de cerrar la parte. No se tocan los umbrales.
- **Revisión del `auditor-multitenant` (sin hallazgos altos), incorporada:**
  - test de volumen desigual con 50.000 fragmentos en otro negocio, con un `EXPLAIN` como
    `app_runtime` que confirma que los fragmentos no se leen con `Seq Scan`;
  - test estático de que `pack_knowledge` no lee el entorno, `get_settings` ni clientes HTTP;
  - el contexto que arma el motor para sus resolvers también lleva la identidad del agente;
  - el docstring de `grabacion` queda alineado con `development`/`test`.
  - Anotado sin cambio: `audit_log.input` guarda la `consulta` tal cual, como en el resto de las
    tools y dentro del negocio.
- **Pendiente:** la eval de punta a punta con el LLM (5 conversaciones) necesita grabar el LLM del
  turno con el pack activo, lo que cuesta LLM real. Queda para cuando el dueño regrabe el gate, junto
  con la 49-5.
