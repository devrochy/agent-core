# ADR-0088 · Un error de negocio no cuenta como falla de salud

- **Estado:** aceptado
- **Fecha:** 2026-10-02
- **Enmienda a:** [ADR-0024](0024-plugin-health-por-tenant.md) (salud de plugin por tenant)

## Contexto

El ADR-0024 acotó el radio del auto-deshabilitado a un tenant, pero la
condición que lo dispara sigue siendo **cualquier excepción** de una tool:
`core/mcp/host.py::execute_tool` envuelve toda excepción no-`PluginError` en
`PluginError`, y `loop.py::_execute_tool_safely` apaga el pack de ese tenant.

Eso mezcla dos cosas distintas (HU-ARCH-18):

- una **falla de infraestructura** (la base no responde, el proveedor se cayó);
- un **rechazo de negocio** (monto bajo el mínimo del proveedor, dato
  faltante, cliente inexistente), que es el resultado legítimo de una tool.

El caso real: un plan barato bajo el mínimo de Wompi apagaba las
suscripciones de ese tenant; una plantilla con una variable mal escrita,
el comercio. El asistente dejaba de ofrecer esas habilidades sin causa.

## Decisión

1. Nueva jerarquía `BusinessError` en `core/shared/exceptions.py`, con
   subclases por dominio (`ValidationError`, `PaymentCapabilityError`,
   `SubscriptionError`). Acepta `code` y `escalate`.
2. `core/mcp/host.py::execute_tool` captura `BusinessError` **antes** del
   `except Exception`: la convierte en `{"error": ...}` (resultado
   controlado) y **no** la re-lanza como `PluginError`. El pack sigue
   habilitado.
3. `core/orchestrator/loop.py::_execute_tool_safely` también captura
   `BusinessError` (cubre tools declarativas que no pasan por el host).
4. Solo una excepción que **no** sea `BusinessError` (ni `PluginError`)
   dispara `auto_disable_for_tenant` (ADR-0024 intacto).
5. `PaymentCapabilityError` (core/payments) pasa a heredar de
   `BusinessError`; `sub_link_pago` y `crear_link_para_cuota` devuelven
   `status: monto_bajo_minimo` en vez de propagar.
6. Una plantilla de mensaje con variable inexistente cae al texto por
   defecto (`_safe_format`), sin apagar el pack.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Que cada tool atrape sus errores | Depende de la disciplina de cada pack — ya falló (mismo motivo por el que se escribió este ADR) |
| Circuit breaker por N fallas | Tolera errores de negocio repetidos: un negocio con muchos rechazos legítimos apagaría su pack |
| Marcar `PaymentRequestRejected` como excepción especial sin jerarquía | Era el estado parcial previo (solo Wompi); no cubre datos faltantes ni suscripciones |

## Consecuencias

**Positivas:** un rechazo de negocio ya no apaga una habilidad; el radio del
ADR-0024 queda reservado a fallas reales; el resultado que ve el LLM es
accionable (`{"error": ...}`, `status`, `escalate`).

**Negativas / lo que aceptamos pagar:** cada pack debe migrar sus errores de
negocio a `BusinessError` (auditoría pendiente en HU-ARCH-18 para
commerce/payments/subscriptions/cobranza/calendar); mientras no lo haga, un
rechazo sin migrar seguirá contando como falla.

**Qué haría que revisáramos esto:** que una falla real se marque por error
como `BusinessError` (enmascararía una caída). Mitigación: `BusinessError`
solo se lanza desde el punto donde la tool decide que el rechazo es de
negocio, y los tests de "falla real lanza `PluginError`" quedan en
`tests/test_business_error_no_apaga_packs.py`.
