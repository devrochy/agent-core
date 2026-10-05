# PROMPT-CACHING-GUIDE · Reglas para no romper el cache

El system prompt se construye en **bloques** (`list[str]`, ver
`core/llm/prompt_builder.py`). El orden y la estabilidad de esos bloques determinan
cuánto pagas por tokens de entrada.

## Los 4 bloques

| # | Bloque | Estabilidad | Cacheable |
|---|---|---|---|
| 1 | Base (identidad + reglas de operación) | Byte-idéntico entre turnos | ✅ |
| 2 | Tono (3 variantes fijas: calido/formal/divertido) | Fijo por variante | ✅ (compartible entre asistentes con el mismo tono) |
| 3 | Voz de marca | Estable por negocio | ✅ |
| 4 | Contexto dinámico (skills del turno + perfil + recall RAG + fecha y hora actual del negocio, siempre la última parte, ADR-0091) | CAMBIA cada turno | ❌ (por eso va al FINAL) |

## La regla de oro

> **NUNCA interpoles fecha, hora, contadores ni ningún valor variable en los
> bloques 1-3.** Un solo carácter distinto invalida el cache de TODO el prefijo,
> silenciosamente, y pagas precio completo en cada turno sin ningún error visible.

Lección documentada de agent-sale: es el "invalidador silencioso" más común.

```python
# ❌ MAL — rompe el cache en cada turno:
block_base = f"Eres {name}. Hoy es {datetime.now():%Y-%m-%d}."

# ✅ BIEN — la fecha va en el bloque dinámico (4) o en el mensaje del usuario:
block_base = f"Eres {name}."
dynamic_block = f"## Contexto del turno\nFecha actual: {today}"
```

## Cómo funciona en cada proveedor

### DeepSeek (default)
Context caching **automático por prefijo**: no configuras nada, pero SOLO aplica si
el prefijo (bloques 1-3 concatenados + primeros mensajes) es byte-idéntico.
- Cache hit: ~10× más barato en tokens de entrada.
- Verificación: campo `prompt_cache_hit_tokens` en la respuesta (el core lo registra
  como `cache_read_tokens` en `llm_usage`).

### Anthropic (alternativa)
`cache_control: {type: "ephemeral"}` por bloque — el `AnthropicProvider` lo pone
automáticamente en cada bloque de system. Hasta 4 breakpoints (usamos 3, queda 1 libre).
- TTL 5 minutos, se renueva con cada hit.
- Verificación: `cache_creation_input_tokens` (primera vez) y
  `cache_read_input_tokens` (hits) en `llm_usage`.

## Qué SÍ va en el bloque dinámico (4)

- Resumen de skills activas del turno (cambia según el SkillRouter)
- Perfil del cliente (Capa 2) y recall semántico (Capa 3)
- Cualquier dato con fecha/hora/estado

## Checklist al tocar el prompt

- [ ] ¿Mi cambio está en `BASE_RULES` o `TONE_BLOCKS`? → debe ser texto 100% estático
- [ ] ¿Necesito datos variables? → van en el bloque 4 (`dynamic_parts` del builder)
- [ ] Después del cambio, verifica en /admin/costos que `cache_read` no cayó a cero
