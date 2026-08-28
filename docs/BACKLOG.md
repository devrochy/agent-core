# Backlog técnico

Hallazgos verificados que **no** están arreglados. Cada uno con su evidencia, para que
no haya que volver a descubrirlos. Origen: auditoría del repo de 2026-08-25.

Cuando arregles uno, bórralo de aquí y menciónalo en el commit.

---

## Alta

**Los costos de Anthropic siempre se registran como 0.0**
`core/orchestrator/loop.py:_record_usage` importa el diccionario `PRICING` de
`core/llm/openai_compatible.py`, que solo contiene modelos de DeepSeek y OpenAI. Los
modelos de Claude no están, así que su costo cae a cero. Existe
`BaseLLMProvider.estimate_cost_usd()` (`core/llm/base.py:37-41`) precisamente para
esto, pero nadie la llama. El panel `/admin/costos` subestima el gasto real.

**La firma del webhook es opcional en la práctica**
`core/gateway/webhooks.py:65` solo verifica el HMAC `X-Hub-Signature-256` si hay
`app_secret` configurado. Sin él, cualquiera que conozca la URL puede inyectar
mensajes. Debería fallar cerrado, o al menos gritar en los logs.

**El pedido de monto alto nunca escala**
`plugins/pack_commerce/pack_commerce/skills.py:989` devuelve `{"escalate": True}`
cuando el monto supera `monto_alto_threshold`. Ningún punto de `core/` lee esa clave:
`core/orchestrator/safe_renderer.py` solo interpreta `_safe_append` e `_image_url`.
La señal se pierde y el LLM solo ve un error genérico.

## Media

**El system prompt promete una herramienta que no existe**
`core/llm/prompt_builder.py:BASE_RULES` dice *"si el cliente pide hablar con una
persona, usa la herramienta de escalamiento"*. No hay ninguna tool de escalamiento
registrada. El escalamiento ocurre por keywords, guardrails, refusal o max_iterations,
nunca por decisión del LLM. `MCPHost.trigger_escalation()` existe
(`core/mcp/host.py:80`) y ningún plugin la llama.

**`BASE_RULES` del core menciona el CRM**
Mismo archivo: el prompt del framework habla de "registrar el lead en el CRM", que es
un plugin concreto. Acopla el core a `pack_commerce`.

**El contenedor corre como root y ensucia el repo del host**
El `Dockerfile` no crea usuario no-root, y `docker-compose.yml` bind-montea
`./plugins`. Al arrancar, el entrypoint hace `pip install -e` de cada plugin y deja
`*.egg-info/` y `__pycache__/` **propiedad de root** dentro del repo del usuario, que
después no puede borrarlos ni reinstalar los plugins desde el host sin `sudo`.
Verificado: tras un `docker compose up`, `rm -rf` sobre el clon falla con
*Permission denied* en `plugins/*/`.
Arreglarlo bien implica un usuario no-root en la imagen con cuidado de que
`pip install -e` siga funcionando sobre site-packages.

**Panel admin sin CSRF ni rate limiting**
Todos los POST de `core/admin/views.py` son formularios sin token. `/admin/login` no
tiene límite de intentos. No hay middleware de seguridad de ningún tipo (el `grep` de
`add_middleware` en `core/` no devuelve nada). `itsdangerous` está en las dependencias
y no se usa.

**`Vector(384)` hardcodeado**
En `core/db/models.py` y en la migración `0001_initial_schema.py`, mientras
`EMBEDDING_DIMENSIONS` es configurable en el `.env`. Usar `text-embedding-3-small`
(1536) exige editar el código y escribir una migración a mano. El docstring de
`core/memory/vector.py:14` lo reconoce.

**63 avisos de longitud de línea**
`E501` está excluido en `pyproject.toml` porque activarlo hoy exigía un reformateo
masivo mezclado con el arreglo del arranque. Abordar aparte, en una rama solo de
formato.

**`mypy` no puede fallar**
`make lint` corre `mypy core cli || true`, lo que anula el `strict = true` declarado en
`pyproject.toml`. Medir primero el volumen (`mypy core cli | tail -1`) y activarlo por
módulos con `[[tool.mypy.overrides]]` antes que de golpe.

**`mod-media-processor` no existe**
`cli/registry.py:25` lo anuncia como plugin conocido y
`core/gateway/webhooks.py:116-123` engancha con él para procesar audio e imagen. No
está en el repo ni en ninguna doc. Sin él, todo mensaje con media recibe
*"[El cliente envió un archivo multimedia que no puedo procesar]"*.

**`plugin scaffold` genera menos de lo documentado**
`docs/PLUGIN-CONTRACT.md` §3 promete `models.py`, `jobs.py`, `alembic.ini` y
`alembic/`. `cli/commands/plugin.py:197-307` genera 5 archivos y ninguno de esos —
justo la parte delicada (el `version_table` separado del ADR-005). La misma promesa se
repite en `sdd/templates/skills-config.md.j2` §4 y `.opencode/agents/plugin-dev.md`.

## Baja

**Código muerto**
`MCPHost.register_resource()` y `MCPResource` (registrados, nunca leídos);
`BaseLLMProvider.stream()` (implementado en ambos providers, el orchestrator nunca lo
invoca); `core/tasks/queue.py` completo (importado por cero archivos);
`MemoryError_` y `GuardrailViolation` en `core/shared/exceptions.py`;
`working_memory.clear()`; `entity.find_by_external_id()`; `connections.get_connection()`;
el evento `ON_CONVERSATION_CLOSED` (declarado, nunca emitido — `conversations.closed_at`
no se escribe nunca).

**El consumer no reclama mensajes pendientes**
`core/orchestrator/consumer.py` usa `CONSUMER = "orchestrator-1"` hardcodeado y no hay
`XAUTOCLAIM`/`XPENDING`. Si un proceso muere entre `xreadgroup` y `xack`, esa entrada
queda pendiente para siempre. Con varias réplicas, todas comparten nombre de consumidor.

**Prompt caching de Anthropic: un breakpoint desperdiciado**
`core/llm/anthropic_provider.py:55-61` marca `cache_control` en **todos** los bloques
de system, incluido el dinámico, que cambia cada turno. Ese breakpoint escribe caché
que nunca se reutiliza. `docs/PROMPT-CACHING-GUIDE.md` describe el comportamiento
deseado (no cachear el bloque 4) y dice "usamos 3, queda 1 libre", que no es lo que
hace el código.

**Documentación con deriva**
- `docs/PLUGIN-CONTRACT.md` §13 prohíbe importar `core.plugin_manager`, y los dos
  plugins de referencia lo importan (`pack_commerce` en `module.py:76`, `skills.py:80`,
  `jobs.py:39`; `mod_google_calendar` en `module.py:32,50`). La lista blanca tampoco
  menciona `core.admin.auth`, `core.config`, `core.db.models` ni `core.shared.crypto`,
  que ambos usan.
- `docs/FIRST-ASSISTANT.md` paso 9: tutorial de barbería que pide "una hamburguesa".
- `docs/MONOLITH-MODEL.md` paso 5 dice reiniciar solo `core-api`; el código reinicia
  tres servicios (`cli/commands/plugin.py:170-175`).
- Los 82 criterios de aceptación de `docs/HU/` están sin marcar aunque la
  funcionalidad está implementada.
- `sdd/.state.json` está commiteado con datos de una prueba ("Barber Bot Test", CRM
  v1.0.0, `skills: []`) que no corresponden al CRM v2 actual.
- `sdd/templates/skills-config.md.j2` y `architecture.md.j2` llevan valores del
  framework escritos a mano ("máx 6 iteraciones", "L1 Redis 30 turnos", la lista de 11
  tablas del core) que no se parametrizan desde el estado.

**Sin `LICENSE`**
`README.md` y `pyproject.toml` declaran MIT; el archivo no existe.
