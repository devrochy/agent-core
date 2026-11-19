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
- Verificación: campo `prompt_cache_hit_tokens` en la respuesta (OpenAI lo da en
  `prompt_tokens_details.cached_tokens`). El core lo registra como `cache_read_tokens` y lo
  guarda en `usage_ledger.cached_tokens`.

### Anthropic (alternativa)
Una sola marca `cache_control: {type: "ephemeral"}`, en el **último bloque fijo** (el
penúltimo de la lista: el último es el dinámico, que va sin marca). Con un solo bloque no
hay marca. Así no se pasa del máximo de 4 marcas de la API, tenga los playbooks que tenga
(adenda HU-AOS-35 de ADR-0117).
- TTL 5 minutos, se renueva con cada hit.
- Verificación: `cache_creation_input_tokens` (primera vez, va a
  `usage_ledger.cache_write_tokens`) y `cache_read_input_tokens` (hits, va a
  `usage_ledger.cached_tokens`).
- Si lo fijo queda por debajo del mínimo cacheable del modelo, Anthropic no cachea nada.

### Con releases (`aos_runtime = v3`)
Los bloques fijos se compilan una vez por `(negocio, release, versión del agente)` y se
reutilizan tal cual en cada turno (`core/agents/compiler.py`). El flujo de atención es el
de la release completa; lo que no está disponible en un turno (plugin caído, conexión que
falta) va como aviso al bloque dinámico, no se quita del flujo fijo.

### Medir
`usage_ledger.input_tokens` es el total de entrada con lo cacheado incluido en todos los
proveedores, y `cached_tokens` solo lo leído (desde la migración 0114; las filas
anteriores tienen `cache_write_tokens` en NULL). Costos › por agente muestra el
porcentaje cacheado.

## Qué SÍ va en el bloque dinámico (4)

- Resumen de skills activas del turno (cambia según el SkillRouter)
- Perfil del cliente (Capa 2) y recall semántico (Capa 3)
- Cualquier dato con fecha/hora/estado
- El aviso de las etapas del flujo sin herramientas disponibles en el turno

## Checklist al tocar el prompt

- [ ] ¿Mi cambio está en `BASE_RULES` o `TONE_BLOCKS`? → debe ser texto 100% estático
- [ ] ¿Necesito datos variables? → van en el bloque 4 (`dynamic_parts` del builder)
- [ ] Después del cambio, verifica en Costos › por agente que el porcentaje cacheado no cayó a cero
