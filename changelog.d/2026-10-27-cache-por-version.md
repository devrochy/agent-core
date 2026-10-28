## Caché por versión de lo compilado y prefijo estable (HU-AOS-35, 2026-10-27)

- **Lo compilado de cada release se arma una vez por versión de agente.** Con `aos_runtime = v3`, la vista
  del agente en foco, el flujo de la release y los bloques fijos del prompt se compilan una vez por
  `(negocio, release, versión del agente)` (`core/agents/compiler.py`). Se guardan en la caché de releases y
  cuentan en su tope de bytes. Ya no se relee de disco ni se revalida en cada turno. Los negocios legacy
  arman el prompt como siempre.
- **El flujo de atención fijo ya no cambia cuando falla una conexión.** Es el de la release completa. Si un
  plugin está caído para el negocio, falta una conexión o una herramienta está desactivada, el bloque
  dinámico lo avisa (playbook del kernel `no-disponible`). Las herramientas que se ofrecen al modelo siguen
  filtradas como antes.
- **Anthropic usa una sola marca de caché**, en el último bloque fijo. Antes marcaba todos los bloques,
  también el dinámico, y con 4 playbooks o más pasaba del máximo de la API.
- **Métrica de tokens cacheados (migración 0114).**
  - `usage_ledger.cached_tokens` guarda solo lo leído de la caché, y la columna nueva `cache_write_tokens` lo
    escrito.
  - `input_tokens` pasa a ser el total de entrada con lo cacheado incluido en todos los proveedores.
  - Corte: las filas anteriores tienen `cache_write_tokens` en NULL. Desde el corte, Costos por modelo, la
    consola de conexiones y el editor muestran más tokens de entrada con Anthropic.
  - El costo y los créditos no cambian, porque siguen saliendo de lo que reporta el proveedor.
  - El proveedor OpenAI-compatible también lee `prompt_tokens_details.cached_tokens`.
- **Costos por agente.** `GET /admin/costs` trae `by_agent`, con los últimos 30 días por agente: turnos,
  tokens de entrada, tokens cacheados, porcentaje cacheado y costo.
