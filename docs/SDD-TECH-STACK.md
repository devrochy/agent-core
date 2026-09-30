# SDD · Stack Tecnológico y Matriz de Decisión

Este documento justifica cada pieza del stack y define el mapa de **portabilidad a
TypeScript** para un futuro `core-ts`. Es un documento de diseño vivo: si cambias una
pieza, actualiza la fila y el porqué.

## Matriz de decisión

| Componente | Elección | Alternativas evaluadas | Por qué ganó |
|---|---|---|---|
| Runtime | Python 3.12+ | Node 22, Go | Ecosistema IA (pgvector, sentence-transformers, SDKs LLM), tipado moderno con mypy strict, y el equipo ya opera FastAPI (agent-dashboard) |
| HTTP | FastAPI | Flask, Django, Litestar | Async nativo (I/O bound: LLM + Graph API), Pydantic integrado (validación = schema del form generator), lifespan para el plugin scanner |
| ORM | SQLAlchemy 2.0 (async) | Django ORM, raw SQL (como agent-sale), Tortoise | Tipado 2.0 excelente, Alembic maduro para migraciones POR PLUGIN, async con asyncpg |
| Migraciones | Alembic | node-pg-migrate (agent-sale), raw SQL | Soporta múltiples árboles con version_table separada → migraciones por plugin (gap real de agent-sale/agent-dashboard) |
| BD | PostgreSQL 16 + pgvector | MySQL, Mongo, vector DB dedicada (Pinecone/Qdrant) | Una sola BD para entidad + vector + JSONB. pgvector es suficiente hasta millones de embeddings. Menos piezas operativas |
| Cache/colas | Redis 7 | RabbitMQ, Kafka, SQS | Ya se necesita para working memory; Streams + ZSet + SET NX cubren cola, debounce e idempotencia. Kafka es sobredimensionado (ADR-002 de agent-sale) |
| Cola de mensajes | Redis Stream + consumer group | Celery para todo | El path del mensaje es latencia-crítico: Stream con XREADGROUP da control fino (re-delivery, DLQ) sin serialización Celery |
| Cron/jobs | Celery + Beat | node-cron (agent-sale), APScheduler, Arq | Beat schedule dinámico (los plugins registran con @register_cron), workers separados del proceso web, acks_late |
| LLM default | DeepSeek (deepseek-chat) | Claude, GPT-4o | $0.27/$1.10 por MTok con function calling nativo. Claude queda a un `.env` de distancia (BaseLLMProvider) |
| LLM razonamiento | deepseek-reasoner | o1, Claude thinking | Mismo proveedor, misma API key. El difficulty router lo usa solo en turnos complejos sin tools (R1 no soporta function calling) |
| Embeddings | sentence-transformers (local) / OpenAI (opción) | Cohere, Voyage | DeepSeek NO tiene API de embeddings (gap que dejó a agent-sale sin memoria vectorial). Local = $0 y sin API key; OpenAI para escala |
| Panel admin | Jinja2 + Bootstrap 5 (SSR) | React SPA (agent-dashboard), HTMX | Cero build step, cero API JSON paralela. El FormGenerator emite HTML directo desde Pydantic. Un dev de plugins no escribe UI |
| Cifrado | cryptography (Fernet) | AES-GCM manual (agent-sale), Vault | Fernet = AES-128-CBC + HMAC con API a prueba de errores. Vault es sobredimensionado para single-tenant |
| CLI | Typer + questionary + rich | Click, argparse | Tipado, subcomandos limpios, wizards interactivos de calidad |
| Logs | structlog (JSON a stdout) | Pino (agent-sale), loguru | JSON estructurado → cualquier agregador (Loki, CloudWatch). Contextvars para conversation_id |
| Auth panel | Sesión en BD + bcrypt | JWT | Revocación inmediata (JWT no revoca — ADR-025 de agent-sale) |

## Sección Portabilidad: mapa Python → TypeScript (futuro core-ts)

El diseño de agent-core es portable porque las abstracciones son conceptuales, no
idiomáticas. Equivalencias directas:

| agent-core (Python) | core-ts (TypeScript) | Notas de porte |
|---|---|---|
| FastAPI | NestJS (o Fastify) | Los routers de plugins → módulos dinámicos de NestJS |
| Pydantic (config + form generator) | Zod + zod-to-json-schema | El FormGenerator lee JSON Schema: idéntico input |
| SQLAlchemy 2.0 | Prisma (o Drizzle) | Los modelos del core son tablas planas sin herencia |
| Alembic (árboles por plugin) | Prisma Migrate por workspace | version_table separada ≈ schema por plugin |
| Celery + Beat | BullMQ + repeatable jobs | @register_cron → `queue.add(name, {}, {repeat: {pattern}})` |
| Redis Stream + XREADGROUP | Redis Stream (ioredis) | API idéntica — agent-sale ya lo hace en TS |
| entry_points (setuptools) | package.json `agentCore.plugin` + import dinámico | El scanner lee node_modules en vez de entry_points |
| structlog | Pino | JSON a stdout en ambos |
| httpx | fetch nativo | agent-sale ya usa fetch para openai-compatible |
| sentence-transformers | @xenova/transformers (ONNX) | Mismo modelo all-MiniLM-L6-v2 disponible en ONNX |
| Fernet | node:crypto AES-256-GCM | agent-sale ya tiene secretBox en TS |

**Contratos que NO cambian entre lenguajes** (son el verdadero SDD):
- `BaseLLMProvider.generate(system_blocks, messages, tools, max_tokens) -> LLMResponse`
- `BaseModule { id, config_schema, on_activate(host) }`
- `MCPTool { name, description, parameters: JSONSchema, handler }`
- `InboundAdapter { verify_request, parse_inbound } / OutboundAdapter { send_text, send_image }`
- El flujo del turno (webhook → stream → consumer → debounce → loop → guardrails → splitter)
- El modelo de datos (mismas tablas, mismos UNIQUE, mismo trigger de audit)

## Costos estimados (tenant pequeño: ~500 conversaciones/mes)

| Rubro | Estimado/mes |
|---|---|
| DeepSeek (~5 llamadas/conv, ~3K tokens in + 500 out) | ~$3-6 USD |
| Embeddings local (sentence-transformers) | $0 |
| VPS 2GB (o Fly.io) para el monolito + Postgres + Redis | ~$6-15 USD |
| WhatsApp (ventana de servicio 24h) | $0 (mensajes de servicio gratuitos desde 2025) |
| **Total** | **~$10-20 USD/mes** |
