# ADR-0023 — pack-payments: coordinación entre packs vía eventos, sin acoplarlos

- **Estado:** aceptado
- **Fecha:** 2026-09-11

## Contexto

Se decidió construir `pack-payments` (pasarela de pagos en línea, inspirado
en la integración con Wompi de `agent-sale`) como el primer requerimiento
nuevo derivado del análisis de ese proyecto hermano. `pack-commerce` ya
tiene `Order.payment_status` (`pendiente|pagado|rechazado`) pero ninguna
tool lo actualiza después de crear el pedido — hoy solo se fija una vez,
al crearlo (`crear_pedido`, `skills.py:900`).

Un pago en línea se confirma de forma **asíncrona**: el cliente paga en la
pasarela, minutos después llega un webhook. En ese momento `pack-payments`
necesita decirle a `pack-commerce` "el pedido X se pagó" — pero la
invariante nº5 de `CLAUDE.md` prohíbe que un pack importe de otro. No hay
precedente de esto en el repo: el único `host.subscribe()` real hoy es un
pack escuchando un evento **del core** (`pack_commerce/module.py:761`,
`ON_CONVERSATION_STARTED`), nunca un pack escuchando a otro pack.

## Decisión

**Los packs coordinan entre sí únicamente a través de `core.shared.events.event_bus`**,
con nombres de evento como strings libres (no una constante compartida
importada de un pack a otro — eso sí violaría la invariante nº5). Cada
pack que emite un evento de dominio documenta el string y el payload en su
propio `docs/PLUGIN-CONTRACT.md`-equivalente (docstring de `module.py`);
cualquier otro pack que quiera reaccionar usa el mismo string literal y se
suscribe con `host.subscribe("nombre.evento", handler)` dentro de su
propio `on_activate()` — exactamente el mecanismo que ya existe
(`MCPHost.subscribe`/`remove_plugin` limpia solo, sin cambios de código).

`pack-payments` define y emite:
- `"payment.confirmed"` — payload `{reference: str, provider: str, provider_reference: str, amount: float}`.
- `"payment.rejected"` — mismo payload.

`pack-commerce` se suscribe a ambos en su propio `on_activate()` y
actualiza el `Order` cuyo `public_order_number` coincide con `reference`
(acoplamiento por **convención de dato**, no de código: `pack-payments` no
sabe qué es una "Order", solo reenvía la `reference` que el llamador le
dio al pedir el link de pago).

**El contrato de proveedor de pago (`BasePaymentProvider`) vive DENTRO de
`pack-payments`** (`pack_payments/providers/base.py`), no en
`packages/contracts` ni en `core/`: hoy solo `pack-payments` lo usa (a
diferencia de `BaseLLMProvider`, que consume el core mismo). Si en el
futuro otro pack necesitara pagos, se decide ahí si el contrato sube al
core — no antes, para no congelar algo con un solo caso de uso real.

**Resolución de tenant en el webhook**: mismo patrón que
`/webhooks/whatsapp/{slug}` (`core/gateway/webhooks.py`) — el pack expone
`admin_router = APIRouter(prefix="/webhooks/payments/wompi")` con una ruta
`POST /{slug}`, resuelve el tenant por slug ANTES de abrir `tenant_scope`
(la tabla propia del pack tiene RLS, no se puede consultar sin tenant
resuelto), recién ahí carga la config de Wompi de ESE tenant
(`plugin_manager.load_config`) y verifica la firma con su `events_secret`.
Este prefijo (`/webhooks/...`) cae en el allowlist público del middleware
de tenant (`core/main.py`) sin tocarlo — mismo mecanismo que ya usa
`admin_router` de `pack-calendar`/`pack-commerce`, con otro prefijo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `pack-payments` importa `pack_commerce.models.Order` directamente | Viola la invariante nº5 explícitamente; además acopla el pack de pagos a un vertical de comercio — no serviría para un futuro pack de citas que también cobre. |
| Subir `Order`/pagos al core | El core no modela dominio de negocio (invariante nº1: solo tablas realmente transversales como `Customer`/`Conversation`); `Order` es de `pack-commerce`. |
| Constante de evento compartida en `packages/contracts` | Es un contrato congelado que exige ADR para cambiar (invariante nº4) por cada evento nuevo entre 2 packs — demasiado costoso para algo que hoy es solo un string acordado por convención. Se documenta en texto, no en tipo. |
| Un solo pack "commerce+payments" en vez de dos | Repite el problema que `pack-commerce` ya tuvo con "todo en un pack": un tenant que no cobra en línea (solo transferencia/contraentrega) no debería tener que instalar ni configurar credenciales de Wompi. |

## Consecuencias

**Positivas:** `pack-payments` se activa/desactiva por tenant de forma
100% independiente (`AgentSpec.packs`, sin código nuevo — ya lo garantiza
`plugin_manager.activate()` desde el fix de hoy); un tenant que no cobra
en línea no paga el costo de tenerlo instalado; el patrón de eventos entre
packs queda establecido para el próximo caso (reseñas, logística, etc. de
la lista de requerimientos).

**Negativas / lo que aceptamos pagar:** la coordinación por string literal
+ convención de dato (`reference` == `public_order_number`) no tiene
verificación de tipos entre packs — un typo en el nombre del evento o en
qué se pone en `reference` falla en silencio (el handler nunca se llama, o
no encuentra ningún `Order`). Mitigado con logs explícitos en ambos lados
si el evento no encuentra nada que actualizar.

**Qué haría que revisáramos esto:** si aparece un tercer pack necesitando
el mismo tipo de coordinación con una forma de payload distinta, o si el
"un handler no encontró nada" empieza a pasar en producción sin poder
diagnosticarlo por los logs — ahí se justifica un contrato tipado en
`packages/contracts` en vez de convención.

## Impacto en el código

- `packages/pack-payments/` (nuevo pack completo).
- `packages/pack-commerce/pack_commerce/module.py::on_activate` — nueva
  suscripción a `"payment.confirmed"`/`"payment.rejected"`.
- `packages/pack-commerce/playbooks/ventas.md` — instrucción de cuándo
  llamar la tool `generar_link_pago` de `pack-payments` (solo si ese pack
  también está activo para el tenant).
