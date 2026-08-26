# ADR-0014 — Formato de playbook y carga just-in-time

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

La invariante #3 exige cero prompts en código: el texto que el modelo ve (tono, reglas,
identidad) debe ser dato versionado, no `.py`. El contrato ya declara el **índice**
(`AgentSpec.playbooks`: nombre + descripción), pero no el **formato interno** de un playbook
ni cómo se carga. Sin fijarlo, cada pack declararía playbooks a su manera y el core no podría
validar nombres ni el contenido (superficie de inyección y renegociación con CULTO).

## Decisión

Un playbook es un archivo **Markdown** `playbooks/<nombre>.md` versionado dentro de un pack
(o en `core/playbooks/` para los defaults del framework). El **nombre es la clave de acceso**:
`load_playbook(tenant, nombre)` solo devuelve contenido si `nombre` está en la **allowlist**
`AgentSpec.playbooks` del tenant. Reglas:

- Frontmatter mínimo `--- name: <nombre> \n description: <texto> ---`.
- Límite de tamaño 8 KB por playbook (evita inyección por bloat).
- El contenido cargado entra como **bloque del system prompt** de la siguiente iteración,
  nunca como mensaje `user` (invariante #7: lo de fuera es dato).
- El ensamblador sustituye placeholders `{assistant_name}`, `{business_description}`,
  `{language}` desde el contexto del tenant.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Playbook como JSON/rows en BD | Correcto, pero el `.md` versionado permite PRs y diff en los packs |
| Cualquier ruta del pack | El LLM podría cargar archivos fuera del índice del tenant: superficie de inyección |
| Texto libre en `AgentSpec` | Cambia el contrato congelado (ADR-0003/0011) sin necesidad |

## Consecuencias

**Positivas:** el índice (`AgentSpec.playbooks`) ES la composición del prompt por tenant; el
core no contiene texto de instrucciones; los packs declaran playbooks con un formato fijo
(Sprint 3).

**Negativas / lo que aceptamos pagar:** el ensamblador formatea placeholders (un mecanismo,
no contenido); un playbook mal formado falla cerrado (no se incluye el bloque).

**Qué haría que revisáramos esto:** que un playbook necesite lógica condicional o variables
por turno — señal de que el contenido está volviendo a ser código.

## Impacto en el código

`core/agents/playbooks.py` (nuevo), `core/playbooks/` (defaults), `core/llm/prompt_builder.py`
(ensamblador), y en Sprint 3 los `SKILL.md` de CULTO como playbooks de pack.