# ADR-0022 — Leads como vista computada de Customer, no una tabla que sincronizar

- **Estado:** aceptado
- **Fecha:** 2026-09-10

## Contexto

`pack_commerce_leads` modelaba el lead como su propia entidad: se creaba
automáticamente al arrancar una conversación (`auto_create_lead` vía el
evento `ON_CONVERSATION_STARTED`, que solo dispara una vez por
conversación nueva) o cuando el LLM llamaba la tool `crear_lead`
(discrecional). En producción real (tenant `yopoldo`) un cliente conversó,
compró e incluso se escaló a un humano sin que nunca apareciera un lead en
el panel: la conversación ya estaba abierta de antes (el evento no
re-dispara), y el LLM nunca llamó la tool. Una vez que existía un lead
auto-creado vacío, tampoco había forma de completarle el nombre:
`crear_lead` se niega a tocar un lead existente y `actualizar_lead` no
aceptaba `name`. El síntoma reportado ("no veo el lead") era consecuencia
de un modelo con múltiples caminos para que la fila nunca se creara o
quedara huérfana, no un bug puntual.

## Decisión

Un lead deja de ser una fila que hay que crear: **todo `Customer` del
tenant es un lead**, con `estado` (con_pedido / escalada / con_cotizacion /
sin_actividad_comercial) y `segmento` (nuevo/ocasional/frecuente/fiel/
inactivo, vía `classify_customer()`, ya portado de `agent-sale`) calculados
en vivo por SQL contra `Order`/`Quote`/`Escalation`/`Conversation`, sin
tabla propia que sincronizar. `pack_commerce_leads` no se elimina: queda
como enriquecimiento opcional (nombre/interés que el LLM o un admin
capturen), nunca como la fuente de verdad de si algo "es" un lead.

Antecedente directo: `agent-sale/docs/fase-23-.../ADR-036-clasificacion-cliente-5-niveles-y-rediseno-leads.md`,
que ya resolvió el mismo problema con el mismo patrón (la vista "Leads" de
ese proyecto es literalmente `SELECT * FROM customers` con columnas
calculadas) — `classify_customer()` en `pack_commerce/skills.py` ya era un
port de la función de clasificación de esa ADR, usado hasta ahora solo
para elegibilidad de promociones.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener `pack_commerce_leads` como fuente de verdad y arreglar su ciclo de vida (disparar el auto-draft de forma más confiable, dejar que `actualizar_lead` complete el nombre, agregar más eventos) | Cada arreglo tapa un síntoma sin tocar la causa: mientras "existir como lead" sea un evento que algo tiene que disparar (el core o el LLM), siempre va a haber una secuencia de eventos que lo deje sin disparar. Ya se intentó parcialmente (fix de `actualizar_lead` en el commit anterior) y no alcanzó. |
| Forzar que el LLM SIEMPRE llame `crear_lead` en el primer turno (vía prompt más estricto) | El LLM ya decide de forma no determinística cuándo llamar tools (mismo patrón de flakiness visto en los evals de catálogo); apostar la existencia del lead a eso es la raíz del problema, no una solución. |
| Customer = lead, calculado en vivo (elegida) | No depende de ningún evento ni del LLM: el `Customer` ya existe determinísticamente desde el primer mensaje (`core/orchestrator/consumer.py::_resolve_conversation`). Reduce el problema de "¿cuándo se crea un lead?" a "¿cuándo existe un cliente?", que el core ya resuelve. |

## Consecuencias

**Positivas:** elimina de raíz la clase de bugs de leads que nunca
aparecían o quedaban huérfanos sin nombre; no requiere migración de
esquema; reutiliza `classify_customer()` ya existente en vez de introducir
un segundo criterio de clasificación paralelo.

**Negativas / lo que aceptamos pagar:** ahora hay dos fuentes de verdad
para el nombre mostrado (`Customer.name`, capturado por el canal, vs.
`Lead.name`, capturado por el LLM/admin) resueltas por un fallback
(`lead.name or customer.name or customer.external_id`) — quien lea el
código tiene que saber que "el lead" no es una entidad separada.
`customer_id` en `Lead` sigue siendo FK lógica (sin constraint física, ver
`models.py`), así que `_leads_save` valida el tenant a mano en vez de que
la BD lo garantice.

**Qué haría que revisáramos esto:** si en algún momento hace falta un
campo propio del "lead" que no tenga sentido colgado de `Customer` (por
ejemplo, un pipeline de ventas con etapas manuales que un admin necesite
reordenar a mano, no derivar de `estado`/`segmento`), ahí sí conviene una
tabla real de nuevo — con este ADR reemplazado, no editado.

## Impacto en el código

- `packages/pack-commerce/pack_commerce/module.py::_leads_list` — reescrita
  para consultar `Customer` + `Order`/`Quote`/`Escalation`/`Conversation`/
  `Message` en vez de `Lead`.
- `packages/pack-commerce/pack_commerce/module.py::_leads_save` — pasa a
  indexar por `customer_id` (upsert en `Lead`), valida el tenant del
  `Customer` antes de tocarlo.
- `ViewDescriptor` de "Leads" (mismo archivo): columnas y filtro de
  `estado` actualizados a los 4 valores nuevos; el form editable pasa a
  `name`/`interest` únicamente (`score`/`status` ya no se almacenan).
- No toca `pack_commerce_leads` (tabla ni migraciones): sigue existiendo
  para el enriquecimiento opcional.
