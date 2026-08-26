# ADR-0009 — Releases de agente versionados

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Editar el prompt o los playbooks directamente en producción produce cambios sin trazabilidad:
no se sabe qué versión respondió, cuándo cambió o cómo volver atrás. Un cambio malo se queda
hasta que alguien lo detecta.

## Decisión

Un release de agente es un objeto versionado (AgentSpec + packs + playbooks). Prohibido
editar prompts en producción. Publicación con canary al 5% y rollback en un paso.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Editar prompts en producción | Sin trazabilidad ni rollback; imposible auditar qué versión respondió |
| Deploy manual por release | Depende de la disciplina de cada quien; sin canary, un error afecta al 100% |
| Sin versionado | No se puede comparar métricas entre versiones |

## Consecuencias

**Positivas:** cada turno se puede atribuir a una versión; canary detecta regresiones en el
5% antes de expandir; rollback en un paso.

**Negativas / lo que aceptamos pagar:** el pipeline de publicación es más trabajo; una
versión canary corre dos AgentSpecs en paralelo (costo de evaluación).

**Qué haría que revisáramos esto:** que el canary nunca detecte un problema real en un ciclo
de 10+ releases — señal de que la muestra o el golden set son insuficientes.

## Impacto en el código

`AgentSpec` (versión como parte de la identidad), registry de releases, pipeline de
publicación canary + rollback (Sprint 3), y el eval-runner como gate de regresión.