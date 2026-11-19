# ADR-0111 — Cambio de plan con precio del servidor y catálogo de planes en el core

- **Estado:** aceptado
- **Fecha:** 2026-10-08
- **Supersede/enmienda:** enmienda ADR-0106 (contrato de suscripciones: `change_plan` y una operación opcional).
- **Contexto:** HU-SUB-07 (defecto 5 de `01-revalidacion/suscripciones-alineacion.md`). Los journeys de HU-SUB-12
  vieron un plan quedar en **$0** porque `sub_cambiar_plan` recibía el precio del LLM.

## Contexto

`sub_cambiar_plan` (tool del core, ADR-0106) recibía `plan_code`, `nombre`, `precio`, `moneda` y `periodicidad`
**del LLM**. El proveedor los escribía tal cual en la suscripción, y ese valor fijaba los cargos y los links de pago
siguientes. Un cliente podía convencer al modelo de bajarle el precio, o el modelo podía equivocarse. Además, el
cambio era inmediato en los dos sentidos: un upgrade no cobraba la diferencia y un downgrade no esperaba al corte.

El core no puede leer el catálogo de commerce (los planes son productos de commerce, ADR-0083) y ningún pack
importa otro (invariante 5).

## Decisión

1. **Catálogo de planes como capacidad del core.** `core/subscriptions` agrega `PlanCatalog` (`plan_por_codigo(codigo)
   → (variant_id, PlanSnapshot) | None`) y `plan_catalog_registry`, con el mismo patrón que pagos y suscripciones.
   Commerce lo registra al activarse: resuelve SKU o `variant_id` contra **su** catálogo y arma el snapshot con la
   regla D-16 (`pack_commerce/planes.py`). El registro resuelve el catálogo de un pack habilitado en el negocio.
2. **`sub_cambiar_plan` solo recibe `plan_code`.** Precio, moneda y periodicidad salen del catálogo del servidor. Si
   el plan no existe o el negocio no tiene catálogo, se devuelve un error de negocio.
3. **Semántica de `change_plan`** (la decide el proveedor):
   - **Upgrade** (precio mayor): se genera un cargo `tipo = cambio_plan` por la **diferencia prorrateada por días**
     del período en curso (`(nuevo − actual) × días restantes / días del período`), con link de pago en línea si la
     pasarela lo acepta. Si no lo acepta, el cargo queda pendiente para cobrarlo por transferencia. Al confirmarse
     el pago, el plan cambia **en el acto** y el período sigue igual.
   - **Desde un plan de $0** (Free en prueba): el cargo es el precio completo y, al pagarlo, arranca un período
     nuevo desde ese día.
   - **Downgrade** (precio menor): queda **programado para el próximo período** (`cambio_programado`), sin reembolso.
     `generar_cargos` lo aplica al empezar ese período.
   - **Mismo precio, otro plan:** cambio inmediato.

   La vista que devuelve lleva `cambio` (`inmediato | pendiente_pago | programado`) y, si hay cargo, su link.
4. **Operación opcional `permite_varias()`** del proveedor. Dice si el negocio permite varias suscripciones por
   cliente (`SubscriptionConfig.varias_por_cliente`, desactivado por defecto). Si no las permite, commerce no crea
   un pedido de otro plan pago para un cliente con una suscripción paga vigente y le indica al asistente que ofrezca
   el cambio de plan. Un proveedor que no la implemente cuenta como «no permite».
5. **Referencia del pedido.** La suscripción guarda `referencia_origen` (número público del pedido), con un índice
   único parcial por negocio. Activar dos veces con la misma referencia devuelve la misma suscripción.
6. **Cierres de dinero:**
   - Solo cambia de plan una suscripción `activa` o en prueba.
   - No hay cambio entre periodicidades distintas, salvo desde un plan de $0, que arranca un período nuevo.
   - No hay cambio entre planes gratis.
   - Un cargo de cambio guarda el período sobre el que se calculó. Si al pagarse la suscripción ya no está activa,
     el período cambió o el cargo estaba anulado, el plan no se aplica: se audita y se avisa a los dueños.
   - Pedir otro cambio, programar un downgrade o pasar de período (`generar_cargos`) anula los cambios pendientes.
   - La idempotencia por pedido es por `(referencia, plan)`: un pedido con dos planes crea dos suscripciones.
7. Los jobs de período (`generar_cargos`, `marcar_vencidas`, `avisos_de_cobro`) y `charge_link` solo miran cargos
   `tipo = periodo`: un cargo de cambio pendiente no vence la suscripción ni se cobra como período.

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Que el LLM siga pasando el precio, con un guardrail que lo compare | El servidor ya conoce el precio; validar lo que dice el modelo es más frágil que no preguntárselo |
| Cambio de plan solo comprando el plan nuevo (pedido de commerce) | Se pierde el prorrateo y el downgrade programado; el cliente paga un período completo de más |
| Que el proveedor importe el catálogo de commerce | Rompe la invariante 5 |

## Consecuencias

**Positivas:**
- el precio de un plan nunca sale del LLM;
- el upgrade cobra lo justo y el downgrade respeta lo pagado;
- el mismo patrón de registro sirve para otro catálogo de planes (p. ej. cobranza, HU-SUB-15).

**Negativas:**
- dos columnas nuevas en cargos (`tipo`, `cambio_plan`) y dos en suscripciones (`referencia_origen`,
  `cambio_programado`);
- los jobs filtran por `tipo`;
- un cargo de cambio pendiente que nadie paga queda abierto: se reemplaza si el cliente pide otro cambio.

**Qué haría que revisáramos esto:**
- HU-SUB-15, con un segundo proveedor que tenga su propio catálogo;
- HU-SH-04, donde un cambio de plan también cambia el plan de plataforma del negocio;
- descuentos o promociones en el cambio de plan.

## Archivos

- `core/subscriptions/__init__.py`: `PlanCatalog`, `plan_catalog_registry` y `permite_varias` opcional.
- `core/subscriptions/tools.py`: `sub_cambiar_plan` solo con `plan_code`.
- `packages/pack-commerce/pack_commerce/planes.py`: catálogo de planes de commerce; bloqueo de un segundo plan.
- `packages/pack-subscriptions/pack_subscriptions/`: `prorrateo.py`, `change_plan`, `referencia_origen`,
  `cambio_programado` y la migración `pack_subscriptions_0006`.

## Adenda (2026-10-11, HU-AOS-08): cotización y monto confirmado

**Estado:** propuesta; requiere la aprobación del dueño antes del merge. La revisó el agente `arquitecto`.
No cambia el esquema ni la conducta de `sub_cambiar_plan`.

- **Dos operaciones opcionales del proveedor, juntas** (mismo patrón que `permite_varias`):
  - `cotizar_cambio(subscription_id, *, variant_id, plan)`: lo que haría `change_plan`, sin efectos
    (`cambio`, `monto_a_pagar`, `precio_nuevo`, `plan_actual`, `plan_nuevo`, `desde`), con los mismos rechazos;
  - `change_plan(..., monto_esperado=None)`: el monto que el cliente confirmó. Se compara en la **misma
    transacción** y con el mismo `ahora` que el cálculo, redondeado como `prorrateo.py` (al peso en COP, al
    centavo en otras monedas). Si no coincide, `SubscriptionError(code="monto_distinto")` y no se hace nada.
  - Un proveedor que no implementa `cotizar_cambio` no soporta el procedimiento; su `change_plan` nunca recibe
    `monto_esperado` (`core.subscriptions.soporta_cotizacion`).
- **Por qué no repite la alternativa descartada** ("el LLM pasa el precio y se compara"): el monto del modelo
  nunca se usa como monto. El cargo sale siempre del cálculo del servidor; el argumento solo ata lo que el
  cliente vio en el botón (HU-AOS-09) a lo que se cobra, y si difiere no se cobra.
- **Las reglas viven en una sola función pura** (`pack_subscriptions.capability.evaluar_cambio`), que usan
  `change_plan`, la cotización y el resolver `plan_destino` del procedimiento.
- **Tools del core solo de procedimiento** (ADR-0106 sigue: un solo juego de `sub_*` en el core):
  `sub_cotizar_cambio_plan(plan_code)` y `sub_aplicar_cambio_plan(plan_code, monto_a_pagar)`. No son
  `always_available` y ningún `flow.json` las nombra: el loop legacy no las ofrece y no entran en los
  bindings del General v3 (`tolerante.candidatas_del_spec`). Las ofrece el motor en la etapa `confirmar` de
  `pack_subscriptions.cambiar_plan` (adenda HU-AOS-08 de ADR-0113).
- **`sub_cambiar_plan` no se ofrece con `tenants.aos_procedures` encendida**: si no, el modelo podría
  saltarse el botón con la tool de siempre (las del core no las gobierna el motor). Con la bandera apagada se
  ofrece como siempre.
