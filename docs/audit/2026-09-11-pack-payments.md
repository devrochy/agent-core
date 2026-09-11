# Auditoría de aislamiento multi-tenant — sesión 2026-09-11 (pack-payments)

**Fecha:** 2026-09-11
**Estado:** Sprint 5 (adelantado) — primer pack nuevo derivado del análisis de agent-sale
**Criterio:** `CLAUDE.md` (invariantes) + RLS por tenant + ADR-0023

**Alcance:** el pack `pack-payments` completo (webhook público nuevo,
coordinación entre packs vía `core.shared.events.event_bus` — primera vez
que se usa entre dos packs, no solo pack→core) y los cambios en
`pack-commerce` para reaccionar a sus eventos.

## Hallazgos

| severidad | archivo:línea | problema | corrección sugerida | estado |
|---|---|---|---|---|
| media | `pack_payments/module.py::_apply_webhook_event` | Sin guarda de idempotencia sobre el EFECTO del webhook: un reintento de red de Wompi (mismo evento, no un duplicado de otro tenant) sobreescribía el estado y re-emitía el evento de dominio en cada reintento. El único suscriptor actual (`pack_commerce`) es idempotente, pero un suscriptor futuro no idempotente duplicaría el efecto. | Comparar el status contra el valor previo antes de emitir; solo emitir en una transición real. | **RESUELTO: `_apply_webhook_event` devuelve `(transaction, status_changed)`, el emit en `wompi_webhook` es condicional a `status_changed`. Verificado en vivo: reintento con el mismo estado → `status_changed=False`, no se emite.** |
| baja | Ninguna tool del repo (`generar_link_pago` incluida) | `MCPTool.scopes` existe desde ADR-0019 pero está vacío en todos lados — preexistente, no introducido por este diff. `generar_link_pago` es la primera tool que mueve dinero real. | Al retomar el manifest de scopes (Sprint 5), empezar por esta tool. | Documentado en `docs/ROADMAP.md` (fila de scopes), no se toca en este PR — es deuda de framework completo, no de este pack. |
| baja | `pack_commerce/skills.py::crear_pedido` | Mensaje de error de `payment_method` inválido no mencionaba el nuevo valor `pago_en_linea`. | Actualizar el string. | **RESUELTO.** |

## Sin hallazgos de severidad alta

El webhook público resuelve el tenant por `slug` de la URL y abre
`tenant_scope` **antes** de cargar cualquier config o verificar la firma —
la verificación siempre usa el `events_secret` del tenant dueño de esa URL,
sin posibilidad de confusión de tenant. El handler cross-pack en
`pack-commerce` corre dentro del mismo `tenant_scope` (heredado por
`await event_bus.emit(...)`, sin task nueva que pierda el contextvar) y
filtra explícitamente por `tenant_id`. La tabla `pack_payments_transactions`
tiene RLS con el patrón estándar y sus índices empiezan por `tenant_id`.
Las credenciales de Wompi son `SecretStr`; no hay ningún log que las
imprima en claro ni que loggee el payload crudo del webhook.
