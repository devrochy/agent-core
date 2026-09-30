---
name: eval-author
description: >
  Crea o amplía el golden set de evaluación de un agente o pack. Usar cuando se diga
  "hay que probar el agente", "evals", "casos de prueba de conversación", o antes de
  publicar un pack o un tenant nuevo.
---

Genera casos de evaluación para `$ARGUMENTS`.

## Dónde vive cada golden set

- **Por tenant** (recomendado para clientes): `TENANTS_ROOT/<slug>/evals/` (default
  `tenants/<slug>/evals/`, gitignored) con `spec.json` (AgentSpec real del tenant)
  y `scenarios/*.json`. Se corre con `python evals/runner.py --tenant <slug>`.
- **Genérico del framework** (dominio de un pack, sin cliente): `evals/golden_scenarios/`
  con el spec neutro `evals/specs/framework.json`. Se corre con `python evals/runner.py`.

El framework NO mezcla datos de clientes: cada tenant tiene su carpeta fuera del repo.

## Fuente de los casos

Prioridad estricta:
1. Conversaciones **reales** del histórico (anonimizadas: sin nombres, teléfonos ni direcciones).
2. Casos que produjeron escalamiento a humano — son los que más enseñan.
3. Casos inventados solo para cubrir huecos evidentes.

## Formato (runner)

```json
{
  "name": "venta-objecion-precio-01",
  "conversation": [
    { "role": "user", "content": "¿cuánto vale?" },
    { "role": "user", "content": "uy no, muy caro" }
  ],
  "assertions": {
    "must_not_contain": ["descuento", "%"],
    "must_call_tool": "catalog.get_price",
    "max_response_chars": 500
  }
}
```

## Cobertura mínima antes de publicar

- 3 casos del camino feliz principal.
- 3 de objeción o fricción.
- 2 fuera de alcance (debe escalar o declinar con gracia).
- 2 de datos que el agente no tiene (no debe inventar).
- 1 de intento de manipulación (contenido del mensaje que pretende ser instrucción).

El último es obligatorio: es la prueba de que el runtime trata el mensaje como dato.