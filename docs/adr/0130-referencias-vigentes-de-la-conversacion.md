# ADR-0130 — Referencias vigentes de la conversación en el prompt

- **Estado:** aceptado
- **Fecha:** 2026-10-10
- **Relacionado:** ADR-0019 (packs habilitados por tenant), ADR-0024 (salud por tenant), ADR-0091 (dato en el bloque dinámico, instrucción en el playbook), ADR-0114 (tarjeta de estado, propuesto), ADR-0119 (verificador)
- **Contexto:** incidente en test, tenant `solvershub`, 2026-10-10

## Contexto

- El historial que relee el modelo es solo texto (`_load_history`). Los resultados de tools de turnos anteriores (`audit_log`) alimentan el verificador y los guardrails, nunca el prompt (ADR-0119).
- Por eso el modelo no tiene el `quote_id` real en el turno siguiente y lo adivina.
- En test llamó `crear_pedido(quote_id="PLAN-PREMIUM")`, `confirmar_cotizacion("OC-0001")` (un pedido de otra conversación) y `confirmar_cotizacion("Q-0001")`.
- Generó 6 cotizaciones en 9 minutos y nunca cerró el pedido.
- ADR-0114 propone una tarjeta de estado con tareas *event-sourced*. Todavía no existe.

## Decisión

Un pack puede aportar las **referencias vigentes** de la conversación: los ids de lo que el cliente está negociando, leídos del estado actual de sus tablas. El core las pone como **dato** en el bloque dinámico del prompt.

1. **Punto de extensión de plugins.** Vive en `core.mcp.types`, no en `packages/contracts`, así que la invariante 4 no aplica.
   - `Referencia(tipo, datos)`.
   - `MCPHost.register_referencias(fn)`, con `fn(conversation_id, customer_id) -> list[Referencia]`.
   - Una fuente por plugin; se retira con el plugin.
2. **Solo datos, saneados y renderizados por el core** (`core/orchestrator/referencias.py`, invariante 7):
   - claves y tipos en `[a-z_]`;
   - valores UUID, número o texto corto de letras, dígitos, `_`, `-` y `.`;
   - lo demás se descarta con un log;
   - la línea `- tipo: clave=valor, …` la arma el core, nunca el pack.
3. **Alcance: la conversación.**
   - Lo que el cliente negocia ahora, de esa conversación y de ese cliente.
   - Un draft de otra conversación no se expone: invitaría a confirmar algo viejo.
4. **Fail-open:**
   - timeout por fuente de 0,5 s;
   - una fuente que falla o tarda se salta con un log;
   - tope de líneas.
5. **Filtros:** solo packs habilitados (ADR-0019) y sanos para el negocio (ADR-0024).
6. **Ubicación en el prompt.** Sección `## Referencias vigentes de esta conversación` del bloque dinámico, antes de la fecha.
   - No toca el prefijo cacheado.
   - No cuenta como evidencia de URLs para el verificador, que excluye el último bloque.
7. **La instrucción vive en el playbook del pack** (invariante 3, como ADR-0091). Para pack-commerce, en `ventas.md`:
   - el `quote_id` sale de ahí o de `generar_cotizacion`;
   - no se regenera una cotización vigente;
   - el `total` de las referencias se puede citar.
8. **pack-commerce:**
   - Aporta la última cotización `draft`/`aceptada` (`quote_id`, estado, total) y el último pedido de la conversación (`order_id`, número público, estado, estado de pago, total).
   - Un `quote_id` que no existe devuelve `quote_id_vigente` con su total y estado en `aplicar_promocion`, `confirmar_cotizacion` y `crear_pedido`.
   - **Nunca la usa en silencio.** Confirmar es aceptar un total concreto.
9. **Cuando exista la tarjeta de ADR-0114**, la absorbe: este hook pasa a ser una de sus fuentes o se retira.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| El core lee `audit_log` y pone los ids en el prompt | Es una foto del pasado: la cotización pudo convertirse o expirar. Además, el core tendría que saber qué claves son referencias (`quote_id`, `order_id`), que es dominio en el core |
| Historial con los mensajes `tool` completos | Multiplica tokens en cada turno y repite JSON viejo (links vencidos, montos superados) |
| Que las tools usen la cotización vigente cuando el id no existe | Confirmaría un total que el cliente no vio. El error con `quote_id_vigente` deja que el modelo compare y reintente |
| Esperar a ADR-0114 | El incidente bloquea las ventas en varios turnos hoy |

## Consecuencias

**Positivas:**
- El modelo tiene el id real en todos los turnos.
- Se cierra el ciclo de cotizaciones repetidas.
- El mecanismo sirve a cualquier pack sin acoplarlos (invariante 5).

**Negativas / lo que aceptamos pagar:**
- Dos lecturas indexadas por turno en los negocios con commerce, y unos 60 tokens en la cola sin caché.
- Un negocio que sobrescribe `ventas.md` (ADR-0026) ve el dato pero no la instrucción, hasta que actualice su playbook.

**Qué haría que revisáramos esto:**
- Que exista la tarjeta de estado de ADR-0114.
- Que un pack necesite exponer texto libre.
- Que la latencia de las fuentes se note en el turno.

## Impacto en el código

- `core/mcp/types.py` (`Referencia`, `ReferenciasFn`) y `core/mcp/host.py` (`register_referencias`).
- `core/orchestrator/referencias.py`, `core/orchestrator/loop.py` y `core/llm/prompt_builder.py` (`PromptContext.referencias`).
- `packages/pack-commerce/pack_commerce/skills.py` (`_cotizacion_vigente`, `referencias_vigentes` y los errores con `quote_id_vigente`), `module.py` (registro y patrones de `cotizacion_aceptada`/`cotizacion_generada`) y `playbooks/ventas.md`.

## Adenda (2026-10-10): pedido de otra compra

Revisada por el agente `arquitecto` antes de escribir código.

**Incidente** (test, tenant `solvershub`, 2026-10-10, 19:34–19:49 UTC). La clienta compró el Basic el 2026-10-05 (pedido `OC-0001`, pagado). En la misma conversación intentó comprar el Premium: había cotización vigente y ningún pedido nuevo. La fuente mostraba `OC-0001` como "pedido" junto a la cotización, y el modelo los unió:
- "te genero uno nuevo para tu pedido OC-0001 por $600.000";
- con el `ya_pagado` de `generar_link_pago` (referencia `OC-0001`): "ya figura como pagado, así que no hace falta generar otro";
- "el pedido figura como cancelado, aunque el pago sí quedó registrado".

**Decisión** (reemplaza el punto 8 en lo que expone pack-commerce):
1. **El más nuevo es la compra en curso.** Entre la cotización vigente y el último pedido de la conversación, gana el más nuevo (`created_at`). Una cotización vigente nunca tiene pedido: `crear_pedido` la pasa a `convertida` en la misma transacción. Por eso `order.quote_id` no sirve para relacionarlos.
   - **Cotización más nueva:** `- cotizacion: quote_id, estado, total, pedido=ninguno`. Si hay un pedido, sale como `pedido_anterior`.
   - **Pedido más nuevo:** la cotización vieja no se expone (§3: invitaría a confirmar algo viejo).
2. **`- pedido:`** solo para un pedido de la compra en curso y activo (`abierto`, `despachado`; pagado y abierto cuenta). Lleva `order_id`, número, estado, estado de pago y total, como antes.
3. **`- pedido_anterior:`** para un pedido de otra compra o cerrado (`cancelado`, `entregado`, `expirado`). Lleva `order_id`, número, estado y estado de pago, **sin total**: no se cita como el total actual. El `order_id` se queda porque un pedido pendiente sigue cobrable con `confirmar_pago_pedido`, que toma el monto del pedido.
4. **Sin datos que son instrucciones.** Se descartó `siguiente=crear_pedido` (invariante 3) y también `cubre_compra_actual=no`, que repite lo que ya dicen el tipo y `pedido=ninguno`. La regla está en `ventas.md` y en el override de solvershub (ADR-0026).
5. **Verificador:** afirmación `pago_registrado` de pack-commerce ("figura como pagado", "el pago quedó registrado", "ya pagaste", "recibimos tu pago"). La respalda `consultar_estado_pedido` o `sub_consultar` en el turno. El `ya_pagado` de `generar_link_pago` es un error y no respalda nada. **No cambia el core** (sin `Afirmacion.respaldo` ni `niega`): los dos casos del incidente se detectan solo con `tools`.
6. **`ya_pagado` (pack-payments):** sin cambios. `generar_link_pago`, la tool que lo devolvía, se retiró (adenda 2026-10-10 de ADR-0087): el modelo ya no elige referencia ni monto de un cobro, y el link del pedido sale de `confirmar_pago_pedido`.

**Lo que aceptamos pagar:**
- Una frase verdadera después del pago ("tu pago quedó registrado") dicha sin consultar el estado en ese turno pide un reintento. Las referencias no son evidencia (ADR-0119). En el set etiquetado es 1 de 218 correctas (0,5 %). Un negocio la apaga con `completion_claims_disabled`.
- **Hueco que no cubre el verificador:** si el modelo consulta `OC-0001` (pagado) y después dice "ya está pagado" refiriéndose al Premium, la frase tiene respaldo. Lo cubren las referencias y el playbook.
- Con el pack de suscripciones y el Basic activo, comprar el Premium es un cambio de plan (`sub_cambiar_plan`), no un pedido nuevo (`crear_pedido` responde `ya_tiene_suscripcion`). Esta adenda no cambia ese camino.
