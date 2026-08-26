---
name: eval-author
description: >
  Crea o amplía el golden set de evaluación de un agente o pack. Usar cuando se diga
  "hay que probar el agente", "evals", "casos de prueba de conversación", o antes de
  publicar un pack o un tenant nuevo.
---

Genera casos de evaluación para `$ARGUMENTS` en `evals/`.

## Fuente de los casos

Prioridad estricta:
1. Conversaciones **reales** del histórico (anonimizadas: sin nombres, teléfonos ni direcciones).
2. Casos que produjeron escalamiento a humano — son los que más enseñan.
3. Casos inventados solo para cubrir huecos evidentes.

## Formato

```json
{
  "id": "venta-objecion-precio-01",
  "turns": [
    { "role": "user", "content": "¿cuánto vale?" },
    { "role": "user", "content": "uy no, muy caro" }
  ],
  "assertions": [
    { "type": "no_contiene", "value": ["descuento", "%"] },
    { "type": "menciona", "value": ["valor", "garantía"] },
    { "type": "tool_llamada", "value": "catalog.get_price" },
    { "type": "no_escala" },
    { "type": "costo_max_usd", "value": 0.04 }
  ]
}
```

## Cobertura mínima antes de publicar

- 3 casos del camino feliz principal.
- 3 de objeción o fricción.
- 2 fuera de alcance (debe escalar o declinar con gracia).
- 2 de datos que el agente no tiene (no debe inventar).
- 1 de intento de manipulación (contenido del mensaje que pretende ser instrucción).

El último es obligatorio: es la prueba de que el runtime trata el mensaje como dato.