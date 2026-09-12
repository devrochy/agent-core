# Roadmap de agent-core

Este fichero es el tablero. Se actualiza al cerrar cada sprint. Claude lo lee para saber
en qué estamos.

**Sprint activo:** Sprint 4
**Última actualización:** 2026-08-26

---

## Sprint 0 — Cimientos · Semana 1

**Objetivo:** que exista un framework que defender, aunque todavía no haga nada.

- [x] `docs/inventory.md` completo y revisado a mano *(completo; revisión manual de Rob pendiente)*
- [x] Monorepo Python (uv/pyproject) con la estructura de `CLAUDE.md`
- [x] `@agentcore/contracts` Python v1.0.0 congelado (ADR-0011) y compilando
- [x] ADR-0004 a 0010 separados en ficheros propios
- [x] Devkit verificado: el hook de RLS bloquea una migración de prueba sin RLS
- [x] `auditor-multitenant` corrido contra el código actual, hallazgos en `docs/audit/`

**Salida:** los contratos compilan y el hook bloquea de verdad (pruébalo a propósito).

---

## Sprint 1 — Núcleo multi-tenant · Semanas 2–3

- [x] Esquema core con `tenant_id` + RLS en todas las tablas
- [x] `tenant_id` como claim del contexto autenticado (auth propia, ADR-0012)
- [x] Gateway: verificación de firma de Meta
- [x] Dedupe por `external_message_id` (ADR-0007)
- [x] Lock + debounce por conversación en Redis (ADR-0006)
- [x] Colas con cuota de concurrencia por tenant
- [x] DLQ con reintento manual desde el panel

**Salida:** test que envía 5 webhooks duplicados en 3 s y produce exactamente 1 respuesta. ✅
`make test-integration`: 4 tests de salida en verde contra el stack real.

---

## Sprint 2 — Runtime · Semanas 4–5

- [x] `AgentSpec` leído desde base de datos (cero prompts en código, ADR-0014)
- [x] Tool loop con límite de llamadas y presupuesto por conversación (ADR-0015)
- [x] 8 tools nativas del core
- [x] Tool `load_playbook` + índice de playbooks (allowlist)
- [x] `usage_ledger` append-only (ADR-0008): una fila por turno con `agent_spec_version`
- [x] Trazas OpenTelemetry con costo por conversación

**Salida:** el panel muestra costo por conversación y por tenant, en vivo. ✅
`/admin/costos` lee `usage_ledger` por tenant + por conversación; `make test-integration` verde (95 tests).

---

## Sprint 3 — CULTO migrado · Semanas 6–7

- [x] CULTO como tenant #1 sin código específico en el core (el core no tiene dominio de CULTO)
- [x] Playbooks de CULTO convertidos a `SKILL.md` (ventas, seguimiento) y copy a config editable
- [x] `crm_*` multi-tenant: tenant_id + RLS + numeración de pedidos por tenant (ADR-0018)
- [x] Golden set de 35 conversaciones del dominio CRM (sintético; real anonimizado pendiente si se aporta)
- [x] Gate de regresión en CI (GitHub Actions, bloquea) + `make gate` (subconjunto crítico)
- [x] Canary + rollback: ADR-0016 (gate + staging + rollback 1 paso) + `agent-core agent rollback`

**Salida:** CULTO listo para producción sobre agent-core. Faltan de producción real: CULTO
corriendo con credenciales reales, baseline de métricas y el corte (canal Meta + tenant CULTO
configurado). **El hito se cierra en producción, no en el código.**

---

## Sprint 4 — Packs y segundo tenant · Semanas 8–9

- [x] `pack-commerce` extraído de CULTO (ADR-0020)
- [x] FORMOTO como tenant #2 sobre el mismo pack
- [x] Registry v1: manifest, instalación, migraciones por tenant, consentimiento de scopes (ADR-0019)
- [x] Selector de tenant en el panel (cookie `ac_tenant` validada + dropdown)
- [x] Editor de AgentSpec en el panel (form por secciones + JSON crudo; versiona y activa)
- [x] Formulario de configuración autogenerado desde JSON Schema (FormEngine en core/admin/form_generator)
- [x] Renderizador de vistas por descriptor (ViewRegistry + /admin/packs/{pack}/{view}; leads del CRM portado)
- [~] `ext-moto-fitment` — **descartado por validación de negocio**: ForMotos es una
      tienda de repuestos/accesorios de moto (no un motor de compatibilidad). En el
      sistema real (agent-sale) la "compatibilidad técnica" es un motivo de escalación
      a humano, no una tabla de reglas. Se cubre con configuración: trigger de
      escalación "compatibilidad" + copy. (Ver docs/audit o ADR-0020 para el razonamiento.)

**Salida:** dar de alta un tercer cliente de comercio toma menos de un día y es 100%
configuración. **Cronometrarlo.** Ese número es la métrica de negocio principal.

---

## Sprint 5 — Extensibilidad y confianza · Semanas 10–11

- [ ] Registro de servidores MCP por tenant con allowlist y circuit breaker
- [ ] Guardrails de runtime: pre-tool, post-generación, presupuesto, rate limit
- [ ] Reporte mensual automático por cliente
- [ ] `docs/security.md` completo

**Salida:** conectar el calendario o el ERP de un cliente sin escribir código nuevo.

---

## Sprint 6 — Paquete comercial · Semana 12

- [ ] SLO publicados y medidos
- [ ] Dashboard de calidad visible para el cliente
- [ ] Contrato + SLA + tratamiento de datos revisados por abogado
- [ ] Backup restaurado una vez, con evidencia
- [ ] Respuestas escritas a las 10 preguntas de venta
- [ ] Onboarding cronometrado por debajo de lo prometido

**Salida:** poder entrar a una reunión con un director de operaciones y responder con
documentos, no con promesas.

---

## Deuda registrada

| Fecha | Deuda | Origen | Prioridad |
|---|---|---|---|
| ~~2026-08-26~~ | ~~Tablas `crm_*` sin `tenant_id`/RLS~~ | ~~Sprint 1~~ | ~~alta~~ — **RESUELTA en Sprint 3 (crm_0002)** |
| 2026-08-26 | `PluginManager.module.config` se cachea en singleton sin scope de tenant (punto 8 del auditor); activación de plugins global (fila fusionada con la de abajo, mismo mecanismo) | Sprint 1 (Fase A) | media — **PARCIAL 2026-09-11: investigado a fondo. `mcp_host` (registro de tools/skills) es GLOBAL al proceso por diseño — `register_skill()` revienta con "Skill duplicada" si se intenta re-registrar, así que `on_activate()` solo puede correr una vez por plugin sin importar cuántos tenants lo usen; una redefinición como "registry por-tenant" real (Sprint 4/5) es un cambio de arquitectura mayor, no un fix de esta sesión. Lo que SÍ era un bug acotado y se corrigió: `activate()` hacía no-op TOTAL para el segundo tenant en adelante (ni siquiera validaba su config) — ahora `load_config()` corre siempre, tenant-scoped, en cada `activate()`, así que una config inválida del tenant B se detecta al activar (no a mitad de una conversación real). El enablement real por-tenant ya vivía en `AgentSpec.packs` (`_enable_pack_for_tenant`), confirmado sin cambios necesarios ahí. `auditor-multitenant` encontró además una race preexistente en la primera activación (dos tenants activando el mismo plugin por primera vez casi a la vez podían pisarse y borrar las tools del que ganaba la carrera) — resuelta con un `asyncio.Lock()` por `plugin_id`, ver `docs/audit/2026-09-11-deuda-roadmap.md`. `self.config` SIGUE siendo un atributo compartido entre tenants (solo válido dentro de `on_activate()`) — advertencia explícita agregada a `docs/PLUGIN-CONTRACT.md` § 6 para que ningún pack nuevo lo use en un handler de tool. Tests: `tests/test_plugin_manager_activation.py`.** |
| 2026-08-26 | Tools runtime sin `scopes` ni manifest de dominios (`agentpkg.yaml`) | Auditoría inicial | media — Sprint 5 (MCP + allowlist). **`auditor-multitenant` (2026-09-11, sesión de pack-payments) señaló a `generar_link_pago` como primer candidato concreto para declarar scopes reales al retomar esto: es la primera tool que mueve dinero (DB + llamada saliente con credenciales privadas).** |
| 2026-08-26 | `ToolContext` runtime sin campo `db` (scoped); plugins usan `get_session()` | Auditoría cierre Sprint 1 | media — alinear con el contrato. **Alcance verificado 2026-09-11: el drift real es con el contrato COMPLETO (`packages/contracts`), no solo `db` — faltan también `tenantId`, `agentId`, `traceId`, `secrets`, `http`, `logger`, `emit()`. Cambiar la forma de `ToolContext` exige un ADR nuevo + bump major (ADR-0003/ADR-0011) y migrar 40 call-sites de `get_session()` directo en `packages/pack-*/`. Se deja para cuando se escriba ese ADR — no es un fix de una sesión.** |
| ~~2026-08-26~~ | ~~Activación de plugins global; las skills no se filtran por `AgentSpec.packs`~~ | ~~Auditoría cierre Sprint 1~~ | ~~media~~ — **RESUELTA: "skills no se filtran por packs" ya estaba resuelto (el orchestrator filtra `active_tools` vía `enabled_packs(spec)`), solo faltaba tacharlo — fila de "activación global" separada y fusionada arriba con la de `PluginManager.module.config` (mismo mecanismo de fondo).** |
| ~~2026-08-26~~ | ~~Clave Redis de idempotencia sin `tenant_id`~~ | ~~Auditoría cierre Sprint 1~~ | ~~baja~~ — **RESUELTA 2026-09-11: riesgo real era mayor a lo que decía la fila (colisión entre tenants que comparten `phone_number_id` en un onboarding → pérdida silenciosa de mensaje, no solo una clave desprolija). `core/gateway/idempotency.py::claim_message_once` ahora incluye `tenant_id` en la clave.** |
| 2026-08-26 | `llm_usage` (por llamada) fuente legacy; `usage_ledger` es la fuente | Sprint 2 (ADR-0008) | baja — **RESUELTO 2026-09-11 el código: se confirmó que nadie lo leía (`/admin/costos` y el dashboard usan solo `usage_ledger`) y se retiró `_record_usage()`/su call site/el import en `loop.py` — ya no se escribe en cada turno. El modelo `LLMUsage` queda mapeado (no se borró la clase ni la tabla) hasta decidir retención de los datos históricos y dar de baja la tabla con una migración — eso sigue pendiente.** |
| 2026-08-26 | Pricing LLM hardcodeado en `openai_compatible.py` (CONFIG) | Sprint 2 | media — mover a configuración admin. **Escondía un bug de facturación activo: `core/orchestrator/loop.py` calculaba el costo importando siempre el pricing de `openai_compatible.py`, sin mirar el provider real — con Anthropic activo, `usage_ledger` (la fuente de facturación, ADR-0008) grababa `cost_usd = 0.00` en cada turno, silenciosamente. RESUELTO 2026-09-11 (ese bug): ahora usa `provider.estimate_cost_usd()` (ya existía sin usar en `core/llm/base.py`), resuelto por-provider. También duplicado en `anthropic_provider.py`, no solo en `openai_compatible.py` como decía el texto original. El pricing SIGUE hardcodeado como catálogo global — no hay UI admin ni concepto de "admin de plataforma" separado del admin de tenant para editarlo sin deploy — esa parte sigue abierta.** |
| 2026-08-26 | Queries del pack CRM sin filtro `tenant_id` explícito (confían 100% en RLS) | Auditoría cierre Sprint 3 | media — defensa en profundidad (o `ToolContext.db`). **Los 4 puntos que encontró `auditor-multitenant` el 2026-09-10 (`_leads_list`/`_leads_save` en PR #5, `calificar_lead`/`actualizar_lead`/`category_save` en PR #6 — ver `docs/audit/2026-09-10-sesion.md`) ya están resueltos. Esa corrida solo auditó el diff de la sesión, no todo `pack_commerce` — la fila sigue abierta hasta que se audite el resto del pack. Alcance medido 2026-09-11: ~24 funciones / ~50 call-sites en `skills.py` y `module.py` de `pack_commerce` más allá de esos 4 puntos (`classify_customer`, `_active_customer_order`, `consultar_inventario`, `recomendar_producto`, `aplicar_promocion`, `crear_pedido`, `agregar_item_pedido`, `consultar_estado_pedido`, `_decrement_stock`, `_campaign_redeemed`, `_get_variant_with_stock`, entre otras) — es trabajo de una sesión dedicada con el patrón ya establecido, no un fix rápido.** |
| ~~2026-08-26~~ | ~~Middleware usa el primer tenant del admin sin selector~~ | ~~Auditoría cierre Sprint 3~~ | ~~media~~ — **RESUELTA: ya estaba resuelto desde Sprint 4 (fila 77 del changelog: "Selector de tenant en el panel — cookie `ac_tenant` validada + dropdown", `core/admin/auth.py` + `core/admin/context.py`), solo faltaba tacharlo. Verificado 2026-09-11.** |
| ~~2026-08-26~~ | ~~`turn_gate._tenant_sems` sin eviction; provider de embeddings singleton~~ | ~~Auditoría cierre Sprint 3~~ | ~~baja~~ — **RESUELTA 2026-09-11: `_tenant_sems` ahora es un `OrderedDict` con cota (`_TENANT_SEM_CACHE_MAX=500`) y poda LRU que nunca evicta un semáforo con un turno activo/en cola (refcount `_active_tenants`). El provider de embeddings se investigó y NO es un problema de aislamiento real: es un cliente stateless (sin secretos ni datos de tenant cacheados) — la única limitación real es que no hay BYOK por tenant para embeddings (a diferencia del LLM principal), que es un cambio de diseño aparte, no esta deuda.** |
| 2026-08-26 | Extracción a `pack-commerce`: rename de 13 tablas + `custom jsonb` (ADR-0004) | Sprint 3 (nota) | alta — Sprint 4. **Verificado 2026-09-11: el rename de las 13 tablas está completo. El schema `custom jsonb` (ADR-0004) también está en las 13 tablas desde la migración `0003_commerce_rename.py` — pero ningún modelo SQLAlchemy lo mapea y no se usa en ningún lado del código. No hay una necesidad de negocio concreta que lo requiera hoy, así que se deja como está (schema listo, uso pendiente de un caso real) en vez de mapearlo sin un consumidor.** |
| ~~2026-09-10~~ | ~~El LLM a veces no llama la tool esperada / longitud en `escalamiento-monto-alto`~~ | ~~Evals, sesión 2026-09-10~~ | ~~media~~ — **RESUELTA 2026-09-11 (PR #8): `recomendar_producto` no era flaky, el escenario probaba un caso que la tool no soporta (rediseñado); `consultar_inventario` reforzado en `playbooks/ventas.md` (~70% de acierto, variabilidad de LLM aceptada); longitud ampliada a 600.** |
| ~~2026-09-10~~ | ~~`evals-nightly.yml`: nunca se confirmó una corrida completa del golden set~~ | ~~Sesión 2026-09-10~~ | ~~baja~~ — **RESUELTA 2026-09-11 (PR #9 + #10): disparada manualmente 3 veces. Causa raíz real (no solo flakiness): `evals/specs/framework.json` nunca cargaba `ventas`/`seguimiento` ni `pack_commerce` en `packs` — NINGÚN eval de venta corrió jamás con esos playbooks activos. Corregido; 20/35→29/35 (57%→83%). Los 6 que quedan son variación de secuencias de 2 tools en un runner de un solo turno (no simula el loop agéntico completo), variación ya aceptada.** |
| ~~2026-09-11~~ | ~~`calificar_lead`/`actualizar_lead`: el modelo a veces DICE en el texto que guardó un dato ("ya guardé tu teléfono") sin haber llamado la tool — afirmación falsa de que algo se persistió~~ | ~~Evals, sesión 2026-09-11 (PR #10)~~ | ~~media~~ — **RESUELTA 2026-09-12: guardrail `crm_completion_check` nuevo (`pack_commerce/module.py`), mismo mecanismo que `_amount_guardrail` pero para acciones en vez de montos — se disparó en producción de test para un caso mucho más crítico primero (ver fila nueva abajo), y se extendió para cubrir también este caso original.** |
| ~~2026-09-12~~ | ~~El modelo dijo "cerré tu pedido... en un momentico el asesor te pasa el link" sin haber llamado `crear_pedido`, `generar_link_pago` NI `escalate_to_human` — el cliente quedaba esperando algo que nunca iba a pasar~~ | ~~Incidente real en test (tenant `yopoldo`), tras corregir el crash de UUID inválido~~ | ~~alta~~ — **RESUELTA 2026-09-12: guardrail `crm_completion_check` — bloquea cualquier respuesta que afirme "pedido confirmado"/"te envío el link"/"te comuniqué con un asesor" sin que la tool correspondiente (`crear_pedido`/`consultar_estado_pedido`, `generar_link_pago`, `escalate_to_human`) haya corrido en ese mismo turno. Tras 1 reintento fallido, el turno escala DE VERDAD (vía `escalate()`), en vez de que el modelo simule haberlo hecho. Cada resultado de tool ahora se etiqueta con `_tool_name` (clave reservada nueva, `core/orchestrator/safe_renderer.py`) para que el guardrail sepa qué tool lo produjo. No es infalible (es texto libre, no cubre cada forma posible de decirlo) pero cierra el caso real encontrado.** |
| ~~2026-09-10~~ | ~~`pack_commerce_categories` sin `deleted_at`~~ | ~~Fix de categorías, sesión 2026-09-10~~ | ~~media~~ — **RESUELTA 2026-09-11 (PR #7, migración `crm_0004` + endpoint de eliminación)** |
| ~~2026-09-10~~ | ~~`sdd/` bloqueaba portar 4 sub-agentes de OpenCode a `.claude/agents/`~~ | ~~Port de tooling, sesión 2026-09-10~~ | ~~baja~~ — **RESUELTA 2026-09-11: `sdd/` NO tenía un bug (es un artefacto de instancia única, como `.env`, generado por `agent-core init`; `tenant create` nunca lo toca). Solo tenía datos huérfanos de una prueba de `init` sin relación con este entorno — borrados. Los 4 agentes portados, con sus rutas internas corregidas (`plugins/` → `packages/pack-*`, `crm_leads` → `pack_commerce_leads`, ya obsoletas desde ADR-0020).** |
| ~~2026-09-11~~ | ~~`agent-core plugin scaffold` genera plugins nuevos en `plugins/<id>/` (heredado)~~ | ~~Sesión 2026-09-11~~ | ~~media~~ — **RESUELTA 2026-09-11: `cli/commands/plugin.py::scaffold` genera directo en `packages/pack-<nombre>/pack_<nombre>/`, mismo layout y convención de nombres que `pack-commerce`/`pack-calendar` (pyproject con `[tool.setuptools.packages.find]`, entry-point `pack_<nombre>`). Probado con un scaffold real y borrado después.** |
| 2026-09-11 | `core/gateway/webhooks.py`: logging `webhook_signature_trace` marcado `# TEMP-DIAG` (diagnóstico puntual del E2E de "yopoldo") sigue en el flujo de producción — imprime `phone_id`, prefijo de la firma HMAC y `app_secret_len` en cada webhook entrante | Encontrado por `auditor-multitenant` el 2026-09-11 sobre el diff de deuda del ROADMAP (preexistente, no lo introdujo ese diff) | baja — no es PII de cliente final, pero es diagnóstico temporal en producción que expone metadata de la firma; bajar a `log.debug` o quitarlo si el E2E ya no lo necesita |
| ~~2026-09-10~~ | ~~`.opencode/` y `.claude/` conviven sin que quede definido cuál es la fuente de verdad~~ | ~~Port de tooling, sesión 2026-09-10~~ | ~~baja~~ — **RESUELTA 2026-09-11: el usuario usa ambas herramientas, así que las dos quedan. Los 9 skills compartidos son symlinks (`.opencode/` → `.claude/`, cero mantenimiento). Sub-agentes/guardas/permisos no se pueden symlinkear (frontmatter y lenguaje distintos) — convención de "replicar a mano" documentada en `CLAUDE.md`.** |
| ~~2026-09-11~~ | ~~`/admin/plugins`: el botón "Activar" (sin pasar por "Configurar") de un pack con campos obligatorios mostraba el `ValidationError` crudo de pydantic como flash~~ | ~~Encontrado en vivo al activar `pack-payments` en test~~ | ~~baja~~ — **RESUELTA 2026-09-11: `core/admin/views.py::_ensure_pack_activated` ahora traduce el fallo de validación a un mensaje accionable ("necesita configuración -- usá el botón Configurar"). Afectaba a CUALQUIER pack con campos obligatorios sin default (ya existía para `pack-calendar`, no solo `pack-payments`), preexistente al pack nuevo.** |
| ~~2026-09-11~~ | ~~`_amount_guardrail` escalaba SIEMPRE que la respuesta mencionaba un precio real de catálogo (no intermitente)~~ | ~~Encontrado en vivo probando `yopoldo` ("Pollo Broaster" escalaba a humano cada vez)~~ | ~~alta~~ — **RESUELTA 2026-09-11: el guardrail solo miraba valores de primer nivel del resultado de la tool; `consultar_inventario`/`recomendar_producto` devuelven el precio anidado en `matches[]`/`recommendations[]`, invisible para el scan viejo. Puerto del algoritmo real de `agent-sale` (`priceGuardrail.ts`: extracción recursiva por NOMBRE de clave — `price/unit_price/line_total/subtotal/total/discount` — no "cualquier número"). También se amplió la ventana de verificación a toda la conversación (vía `audit_log`, no solo el turno actual) para permitir confirmar un precio ya establecido sin re-llamar la tool — mismo bug que agent-sale ya había encontrado y resuelto. Nunca lo agarraron los evals porque `evals/runner.py` no corre los guardrails del loop real (limitación ya conocida).** |
| 2026-09-11 | `crear_pedido` no tenía el chequeo de `monto_alto_threshold` que sí tiene `agregar_item_pedido` — un pedido inicial de monto alto se creaba sin ningún chequeo | Encontrado al comparar con `crearPedido.ts` de agent-sale (chequeo antes de insertar) | media — **RESUELTO el gap de paridad** (mismo patrón que `agregar_item_pedido`: devuelve `{"error": ..., "escalate": True}` antes de insertar el `Order`). Comparte la misma limitación que la fila de `calificar_lead`/`actualizar_lead` de arriba: depende de que el LLM note el flag `escalate` y llame `escalate_to_human`, todavía no es determinístico — ese guardrail (tipo `_amount_guardrail` pero para "se ofreció escalar y no se hizo") sigue siendo trabajo aparte. |
| ~~2026-09-11~~ | ~~Un `variant_id`/`quote_id`/`order_id` inválido inventado por el LLM tiraba una excepción no manejada (`asyncpg.DataError`) que el core interpretaba como falla real del plugin~~ | ~~Incidente real en test: el LLM inventó `variant_id="pollo_broaster"` en `generar_cotizacion` en vez del UUID real de `consultar_inventario`~~ | ~~alta~~ — **RESUELTA 2026-09-11: gravedad real mayor a la esperada — `PluginRecord.status` NO es por-tenant, así que la excepción no manejada disparó `plugin_manager.auto_disable("pack_commerce")` y apagó el pack para TODOS los tenants del proceso (`formoto`, `hueco`, `yopoldo`), no solo para la conversación que la causó. `pack_payments` quedó en error también por una causa aparte (config faltante para el tenant default usado por `activate_all_configured()` al reiniciar). Reactivados ambos manualmente. Fix real: `_parse_uuid()` nuevo en `pack_commerce/skills.py`, usado en todo handler que recibe un ID de tipo UUID como argumento (`generar_cotizacion`/`agregar_item_pedido` vía `_get_variant_with_stock`, `recomendar_producto`, `aplicar_promocion`, `crear_pedido`) — un ID inválido ahora devuelve un error recuperable por el LLM en vez de una excepción.** |
| 2026-09-11 | `auto_disable` apaga el pack ENTERO para todos los tenants ante cualquier excepción no manejada, sin distinguir "falla real de infraestructura/bug del plugin" de "argumento inválido de una tool puntual" | Encontrado durante el incidente de arriba, severidad confirmada **alta** por `auditor-multitenant` (2026-09-11): esta es la causa raíz real del apagón, no el parseo de UUID en sí | alta — el fix de `_parse_uuid`/validación de tipos evita los vectores YA encontrados, pero cualquier excepción nueva no prevista (de este pack o de uno futuro) puede repetir el apagón global mientras `PluginRecord.status` siga siendo una fila por `plugin_id` sin `tenant_id`. Cambiar esa semántica (ej. degradar a "error solo para el tenant de esta invocación" cuando el `ToolContext` tiene tenant_id resuelto) toca el contrato de plugins (`docs/PLUGIN-CONTRACT.md`) y `core/mcp/host.py`/`core/plugin_manager.py`, amerita un ADR aparte — no se resuelve en esta tanda. |
| ~~2026-09-11~~ | ~~3 variantes más del mismo patrón (argumento mal tipado -> excepción no manejada -> mismo apagón global) encontradas por `auditor-multitenant` sobre el fix de arriba~~ | ~~`calificar_lead` (score no numérico), `generar_cotizacion`/`agregar_item_pedido` (item de la lista que no es un dict), `crear_pedido` (`customer_data` que no es un dict)~~ | ~~media~~ — **RESUELTAS 2026-09-11, misma tanda: las 3 devuelven ahora un error recuperable por el LLM en vez de propagar la excepción.** `pack-payments`/`pack-calendar` fueron revisados por el mismo auditor y NO tienen hoy este patrón (no reciben IDs de tipo UUID como argumento de tool contra su propio ORM). |

| ~~2026-09-12~~ | ~~`activate_all_configured()` (al arrancar) usaba SIEMPRE `default_tenant()` para activar TODOS los plugins por igual~~ | ~~Confirmado 3 veces en test: `pack_payments` (config solo de `yopoldo`) quedaba en `status='error'` en cada reinicio, porque el tenant por defecto no tiene esa config~~ | ~~media~~ — **RESUELTA 2026-09-12: `activate_all_configured()` ahora busca un tenant real con config guardada para CADA plugin (`_tenant_with_config`, itera `tenants` — tabla de sistema sin RLS — probando cada uno dentro de su propio `tenant_scope`, ya que `plugin_configs` sí tiene RLS) en vez de asumir que el tenant por defecto sirve para todos. Cae a `default_tenant()` solo si ningún tenant tiene esa config.** |

| ~~2026-09-12~~ | ~~`WompiProvider.create_payment_link` no mandaba `collect_shipping` (Wompi lo exige, 422 real) ni chequeaba el mínimo real de Wompi ($150.000 COP) antes de llamar~~ | ~~Incidente real en test: pedir el link de pago de un Pollo Broaster ($60.000) rechazaba SIEMPRE y apagaba `pack_payments` globalmente (mismo patrón de `auto_disable` ya documentado)~~ | ~~alta~~ — **RESUELTA 2026-09-12: body corregido contra el sandbox real + la implementación ya probada de `agent-sale` (`collect_shipping: false`, `expires_at` calculado, sin campo `reference` — Wompi no lo usa). Nuevo `PaymentRequestRejected` (no `PluginError`) para rechazos de negocio de Wompi/config faltante — ya no dispara `auto_disable`. `MIN_AMOUNT_COP=150_000` chequeado ANTES de llamar a la red.** |
| ~~2026-09-12~~ | ~~`parse_webhook_event` correlacionaba por `transaction.id` (distinto en cada intento de pago) en vez de `transaction.payment_link_id` (el id del link reusable, lo que se guarda al crearlo)~~ | ~~Encontrado al comparar con `wompiWebhookHandler.ts` de agent-sale — bug silencioso: ningún webhook real de Wompi iba a encontrar jamás la transacción que originó el pago~~ | ~~alta~~ — **RESUELTA 2026-09-12: corregido a `payment_link_id`, con test que reproduce el bug exacto (`transaction.id` distinto de `payment_link_id` en el mismo payload).** |
| ~~2026-09-12~~ | ~~El guardrail `crm_completion_check` (de ayer) fue esquivado por el propio modelo en un incidente posterior el mismo día, cambiando de conjugación ("Cerramos" en vez de "Cerré", "genero el link" en vez de "envío/paso")~~ | ~~Incidente real en test, mismo día~~ | ~~alta~~ — **RESUELTA 2026-09-12: patrones reescritos por raíz de verbo (`cerr\w*`) y por tema (`link de pago`/`para pagar`) en vez de frase exacta — más robusto a variación de conjugación, aunque sigue sin ser infalible (texto libre).** |
| 2026-09-12 | Mensajes del asistente con formato Markdown (`**negrita**`) que WhatsApp no interpreta — el cliente ve los asteriscos literales | Reportado por el usuario probando el flujo de pedido | media — **RESUELTO con dos capas: regla explícita en `core/playbooks/reglas-operacion.md` (no confiar solo en el prompt) + `strip_markdown()` determinístico en `core/orchestrator/loop.py` aplicado a toda respuesta final, igual criterio que los guardrails de esta sesión.** |

| 2026-09-12 | `generar_link_pago` no declara `scopes` (ADR-0019) pese a hacer red externa + escritura; `MIN_AMOUNT_COP` hardcodeado (podría variar por tenant); dominios de Wompi no están en ningún `agentpkg.yaml` (ese manifest no existe todavía en el repo) | `auditor-multitenant` sobre el fix de Wompi, 2026-09-12 | baja — deuda preexistente/de diseño futuro, no bloqueante; se retoma junto con la fila de `scopes`/manifest ya registrada arriba (Sprint 5) |

Nota aparte (no un hallazgo del auditor): en el CI del PR de Wompi real
(2026-09-12), `venta-recomendar-producto-03` falló 4 veces seguidas antes
de pasar (mismo síntoma ya aceptado — llama `consultar_inventario` en vez
de `recomendar_producto`, ~70% de acierto documentado arriba). Una racha
de 4/4 fallos es estadísticamente rara para ese ratio (~1%). Sin conexión
lógica encontrada con ese diff (los cambios de playbook eran sobre
markdown y el mínimo de Wompi, secciones no relacionadas). Se anota por si
la racha se repite y amerita remedir el ratio real.

Cada vez que un onboarding requiera escribir código, se anota aquí: es un pack incompleto.
