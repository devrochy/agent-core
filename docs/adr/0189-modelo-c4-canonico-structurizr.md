# ADR-0189 — Modelo C4 canónico en Structurizr DSL

- **Estado:** aceptado (2026-11-09, por decisión del dueño. Modelo y exportación mergeados en #734; el CI de #737 valida el modelo y falla si `c4.md` queda sin regenerar, comprobado en verde y en rojo en CI real. Sin comprobar por el asistente: cómo se ven los diagramas en GitHub).
- **Fecha:** 2026-11-09

## Contexto

Había tres versiones del C4 que no coincidían: `docs/architecture/c4.md` (sin panel, APK, landing ni Twilio), SPEC-26 §3.4 y §3.5 en `solvershub-docs` (mezcla estado actual y objetivo) y la wiki §2.3 (sin fecha ni git). Eran Mermaid dibujado a mano, sin un modelo detrás, de modo que cada cambio había que repetirlo en cada diagrama.

## Decisión

La arquitectura se modela en un único archivo, `docs/architecture/workspace.dsl`, en Structurizr DSL, y todas las vistas se generan de él.

- `docs/architecture/c4.md` y la wiki §2.3 contienen exportaciones Mermaid producidas por `scripts/export_c4.py`, entre marcadores, y no se editan a mano.
- SPEC-26 §3.4 y §3.5 quedan como registro del diseño y enlazan al modelo para el estado actual.
- El despliegue se modela por entorno. Lo que aún no existe lleva la etiqueta `Objetivo` y su propia vista.
- El modelo se valida con el MCP oficial de Structurizr, corriendo en local con Docker, sin enviar la arquitectura a terceros.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| LikeC4 | Sin notación C4 predefinida (hay que escribirla) y su MCP solo lee el modelo |
| Solo Mermaid | Sin modelo: los diagramas volverían a desalinearse |
| Figma, Miro, Lucid, Excalidraw | Dibujos sin modelo, fuera de Git |
| Kroki o la URL pública de Structurizr | Envían el contenido a un tercero y hay alternativa local |

## Consecuencias

**Positivas:** una sola fuente versionada junto al código, jerarquía C4 validada, y cambios de arquitectura y diagrama en el mismo PR. Mermaid no requiere herramientas nuevas para leerse.

**Negativas / lo que aceptamos pagar:** regenerar requiere Docker y un paso manual; el CI valida el modelo y falla si `c4.md` quedó sin regenerar, con la imagen del MCP en versión fija. El layout automático de Mermaid es peor que un diagrama ajustado a mano. La exportación necesita dos correcciones, aplicadas en `scripts/export_c4.py`. Los packs corren en dos contenedores, pero el modelo los ubica en `core-api`.

**Qué haría que revisáramos esto:** si durante dos trimestres `workspace.dsl` queda atrasado frente a los ADR de topología o si la exportación Mermaid deja de ser legible en GitHub, se reconsidera el modelo o el formato de salida.

## Impacto en el código

Nuevos: `docs/architecture/workspace.dsl` y `scripts/export_c4.py`. Regenerado: `docs/architecture/c4.md`. Sin cambios en el runtime.
