# SAFE-RENDERER · Datos sensibles nunca pasan por el LLM

## El problema

Un LLM puede transcribir mal un número de cuenta, "redondear" un precio o corromper
una URL de pago. agent-sale lo sufrió y lo resolvió con un principio que agent-core
adopta como regla del framework:

> **El LLM anuncia; el core adjunta.** El contenido exacto de datos críticos jamás
> se genera ni se copia por el modelo.

## El mecanismo

Las tools devuelven claves reservadas que el orchestrator procesa DESPUÉS del LLM:

```python
async def crear_link_pago(self, arguments, context) -> dict:
    link = await wompi.create_payment_link(...)   # fuente de verdad
    return {
        "status": "created",
        "amount": 35000,
        # ↓ el core adjunta esto VERBATIM al final de la respuesta:
        "_safe_append": f"💳 Paga aquí de forma segura: {link.url}",
        # ↓ y esta imagen va en la última burbuja:
        "_image_url": link.qr_image,
    }
```

Flujo interno (`core/orchestrator/safe_renderer.py`):

```
resultado de la tool
  ├─ strip_reserved_keys() → lo que VE el LLM (sin _safe_append/_image_url:
  │                          no puede parafrasear lo que no ve)
  └─ extract_safe_extras() → lo que el core ADJUNTA determinísticamente
                             a la respuesta final, después de los guardrails
```

El LLM ve `{"status": "created", "amount": 35000}` y responde algo como
*"¡Listo! Te dejo el link de pago aquí abajo 👇"*. El link real lo pone el core.

## Cuándo usarlo (checklist para autores de plugins)

| Dato | ¿SafeRenderer? |
|---|---|
| URLs de pago, links únicos con token | ✅ SIEMPRE |
| Números de cuenta bancaria / datos de transferencia | ✅ SIEMPRE |
| Códigos de confirmación, números de pedido | ✅ SIEMPRE |
| Links de calendario/eventos generados | ✅ Recomendado |
| Precios finales de una transacción | ✅ Recomendado (y además valida con un guardrail) |
| Descripciones de producto, textos conversacionales | ❌ No — eso es trabajo del LLM |

## Defensa en profundidad: SafeRenderer + Guardrail

SafeRenderer evita que el LLM corrompa el dato exacto. El guardrail (ver
`_amount_guardrail` en mod_crm_engine) evita que el LLM INVENTE cifras que ninguna
tool devolvió. Se usan juntos:

```
Tool → monto real 35000 (+ _safe_append con el link)
LLM  → "Son $35.000, te paso el link"     ← guardrail verifica que 35000 vino de la tool
Core → adjunta el link verbatim            ← safe renderer
```
