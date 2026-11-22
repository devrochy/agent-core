## Todo el gasto del negocio en créditos, con tarifario v2 (2026-11-22, HU-BILL-21)

- **Tarifario `v2`** (`core/billing/tarifario.py`, ADR-0208 §14). Rige desde `vigente_desde` = 2026-11-22 00:00 UTC, una fecha fija en código. Esa fecha tiene que ser anterior o igual al despliegue. Mantiene el margen ×3, la TRM por setting y los fees de `v1`: turno 0,2, nota de voz 0,5, imagen 1,0 y plantilla 0,3. `agent_run`, `embedding` y `tts` no llevan fee. `v1` no cambia. `creditos_congelados` es la única fórmula «costo → créditos» de todo lo que escribe en el ledger.
- **Créditos congelados al escribir** (§11):
  - `agent_run`, en `a2a.liquidar`, con `Gasto.key_mode`.
  - Embeddings de la ingesta de conocimiento. Antes los absorbía la plataforma.
  - `stt` y `vision` de la ingesta de WhatsApp, que salen de la sombra y cobran el costo real más el fee por fila.
  - Una fila nueva `kind='tts'` para la voz de pack-voice. Se escribe con el servicio `core/media/consumo.py` apenas OpenAI cobra.
  - Precios de TTS en `core/media/catalog.py`.
  - **Migración 0153:** suma `'tts'` al CHECK `ck_usage_kind`. Es aditiva.
- **Sin cobro doble y sin cambiar el pasado** (§12). `medir_creditos` cobra el fee de voz e imagen desde `messages` solo para los mensajes anteriores a `vigente_desde`. Las plantillas siguen cobrando desde `messages`. Las filas con `credits` NULL se interpretan con `v1`. Un ciclo cerrado antes de `v2` mide lo mismo que antes.
- **Caché** (§13). `estimate_cost_usd` acepta `cached_tokens` y `cache_write_tokens` y los cobra a su precio, que se agrega por modelo con su fuente en DeepSeek, OpenAI y Anthropic. En Anthropic, la caché leída y escrita ahora se cobra; antes salía en 0. La firma sigue aceptando dos argumentos. El replay de las evals costea igual que el proveedor real.
- **Playground** (§15). Es gratis hasta 300 créditos por ciclo (`PLAYGROUND_GRATIS_POR_CICLO`, D-79), contando solo sus filas desde `vigente_desde`. Lo que pasa de 300 suma al consumo, en `by_concept.playground` y en el renglón sin agente. `eval` sigue sin contar.
- **Ingesta de conocimiento** (§16). Subir o reprocesar un documento verifica `alcanza` antes de embeber y responde 409 `plan_limit_creditos`. La cota es de un carácter por byte, con un mínimo de 0,1 créditos. `agent_run` y TTS no se cortan, solo cuentan.
- **Contrato** (`docs/openapi.json`):
  - `CreditUsage.playground`, nuevo y opcional, con `{used, free_limit, free_remaining, charged}`.
  - `by_concept` suma las claves `tts` y `playground`.
  - 409 `plan_limit_creditos` en `POST …/documents` y en `…/reprocess`.
