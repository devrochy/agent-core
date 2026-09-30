---
name: adr
description: >
  Registra una decisión de arquitectura como ADR numerado. Usar cuando se diga "decidimos",
  "vamos a usar X en vez de Y", "por qué elegimos", o cuando un cambio afecte contratos,
  aislamiento entre tenants, límites de capa o topología de despliegue.
---

Crea `docs/adr/NNNN-<slug>.md` con el siguiente número libre de la secuencia.

```markdown
# ADR-NNNN — <Título>

- **Estado:** propuesto | aceptado | reemplazado por ADR-XXXX
- **Fecha:** YYYY-MM-DD

## Contexto
Qué problema fuerza una decisión. Hechos, no opiniones. 5 líneas máximo.

## Decisión
Qué se decide, en presente y en una frase. Luego el detalle.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|

## Consecuencias
**Positivas:** …
**Negativas / lo que aceptamos pagar:** …
**Qué haría que revisáramos esto:** condición concreta y observable.

## Impacto en el código
Archivos, paquetes o contratos afectados.
```

## Reglas

- Un ADR por decisión. Si tiene dos decisiones, son dos ADRs.
- La sección "qué haría que revisáramos esto" es obligatoria: un ADR sin condición de
  revisión es dogma, no ingeniería.
- Nunca se edita un ADR aceptado. Se escribe uno nuevo que lo reemplaza y se actualiza
  el estado del anterior.
- Si la decisión contradice `CLAUDE.md`, actualiza también `CLAUDE.md` en el mismo commit.