# ADR-0195 — El flujo de atención es del agente y se compila por agente

- **Estado:** propuesto (se acepta al mergear el primer PR de [SPEC-46](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-46-flujo-de-atencion-por-agente/spec.md), cuando el runtime lea el flujo del agente en foco).
- **Fecha:** 2026-11-14
- **Contexto:** [SPEC-46](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-46-flujo-de-atencion-por-agente/spec.md), [D-72](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-72--el-flujo-de-atención-es-del-agente-y-la-configuración-se-reparte-por-nivel) y [REQ-AOS-26](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/requerimientos.md), con [HU-AOS-74](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-46-flujo-de-atencion-por-agente/hu/HU-AOS-74-flujo-editable-por-agente.md), [HU-AOS-75](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-46-flujo-de-atencion-por-agente/hu/HU-AOS-75-release-y-runtime-con-el-flujo-del-agente.md) y [HU-AOS-76](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-46-flujo-de-atencion-por-agente/hu/HU-AOS-76-configuracion-repartida-por-nivel.md).
- **Usa:** D-35 (se venden agentes), ADR-0054 (flujo declarativo), ADR-0109 (composición multi-pack), ADR-0113 (procedimientos y AgentSpec v3), ADR-0117 (releases inmutables y caché por versión), ADR-0190 (habilidades y configuración por agente) y las invariantes 4 (contratos solo por ADR), 5 (un pack no importa de otro) y 8 (sin borrados físicos).

## ADRs que reemplaza o enmienda

| ADR | Relación | Qué cambia |
|---|---|---|
| **ADR-0054** (flujo por pack y tenant) | **Enmienda** | El flujo del negocio deja de ser la fuente de la conversación; el flujo efectivo de cada agente sale de su propio flujo versionado |
| **ADR-0109** (composición multi-pack) | **Enmienda** | La composición `core + packs + tenant` se conserva solo como semilla al crear el agente y para releases anteriores. En runtime no se compone por negocio |
| **ADR-0113** (procedimientos y AgentSpec v3) | **Enmienda** | Los procedimientos abiertos (`=0.0.0`) se derivan del flujo del agente, no del flujo del negocio. El contrato `agentspec/3` no cambia |
| **ADR-0190** (habilidades por agente) | **Nombra** | El flujo completa el reparto por nivel: el agente ya tiene habilidades, configuración, modelo y acciones; le faltaba el flujo |
| **ADR-0117** (releases) | **Nombra** | El manifiesto congela el flujo de cada agente y su hash; el flujo entra en el diff y en la caché por versión |

Ningún ADR aceptado se edita: al aceptarse este, ADR-0054, ADR-0109 y ADR-0113 reciben una nota de estado.

## Contexto

El flujo de atención es hoy un artefacto del negocio. `core/agents/flow.py` compone `core/flow.json`, los `flow.json` de los packs habilitados y el `flow.json` del tenant; `core/orchestrator/loop.py` lo resuelve una vez y `core/releases/build.py` lo congela como `flow_tenant` en el manifiesto. Todos los agentes de un negocio comparten ese flujo.

Al mismo tiempo, el agente es la unidad real: `AgentSpec v3` tiene `persona`, `model`, `procedures`, `tools`, `guardrails`, `knowledge` y `memory`, con borrador y versiones propias (`agent_drafts`, `agent_versions`), y [ADR-0190](0190-habilidades-por-agente.md) ya le dio habilidades y configuración de pack. La pantalla «AgentSpec y proveedor» de Configuración mezcla, en un solo lugar, el AgentSpec del negocio, el proveedor/BYOK, los guardrails, el flujo y las evals.

El dueño decidió el 2026-11-14 que **cada agente define su flujo** (D-72). Dos agentes de ventas pueden compartir catálogo, pedidos y numeración, pero atender distinto.

## Decisión

1. **El flujo es del agente.** Es un artefacto operativo versionado con el agente, **fuera** del `AgentSpec v3`: se guarda en `agent_drafts.flow` y `agent_versions.flow` (JSONB) con `flow_hash`. El contrato congelado no cambia (`extra="forbid"`).
2. **Forma y límites.** `{"sections": [{"id", "label", "purpose", "tools": []}]}`. Se reutilizan `MAX_SECTIONS` (20), `MAX_TOOLS_PER_SECTION` (40), `MAX_FLOW_BYTES` (64 KiB) e ids `^[a-z0-9_]{1,40}$` de `core/agents/flow.py`. No hay `pack` por sección: dentro de un agente no hay colisión entre packs.
3. **Semilla y propiedad.** Al crear un agente se copia el flujo por defecto de su pack (las secciones de `compose_flow` para sus packs, sin las del negocio). Es una copia, no una herencia en vivo: desde ese momento el agente es independiente y puede quitarla o cambiarla. Las secciones `saludo` y `escalamiento` del core se aplican siempre y no se guardan por agente.
4. **Validación.** Al guardar y al publicar, cada tool debe existir en el catálogo del agente (`catalogo_de_acciones`) y estar concedida por su versión. Una sección con una acción inexistente o apagada devuelve 422 con `sections[i].tools[j]`; no se guarda a medias.
5. **API.** `GET/PUT /admin/agents/{id}/flow` con `lock_version` compartido con el borrador, `GET .../flow/options` (acciones disponibles y semilla) y publicación conjunta con el spec. El JSON crudo del AgentSpec sigue siendo solo master.
6. **Release por agente.** El manifiesto lleva `flows: {agent_key: {sections, hash}}`. `build.py` lee el flujo de la versión de cada agente; `derivar_procedimientos` y `_resolver_componentes` resuelven los procedimientos abiertos por agente; `compiler.py` compila con `flow_agente`; `cargar.py` pasa el flujo congelado del componente. `flow_tenant` se conserva **solo para releases anteriores** y deja de escribirse en las nuevas.
7. **Runtime.** `core/orchestrator/loop.py` usa el flujo compilado del agente en foco. Sin release, el fallback por negocio se retira con [ADR-0191](0191-un-solo-motor-retiro-del-runtime-legacy.md) y [HU-AOS-69](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-26-agent-os/hu/HU-AOS-69-retiro-del-camino-legacy.md); mientras exista, un agente sin flujo cae a `flow_tenant` para no cambiar la conducta.
8. **Reparto por nivel.** Agente: persona, modelo y temperatura, guardrails, flujo, conocimiento, acciones y versiones. Negocio: moneda, numeración, catálogo/pedidos, horarios, datos compartidos, textos de cron e intake. Cuenta/plataforma: proveedor y llave BYOK, cuentas Wompi, evals técnicas y JSON crudo. El BYOK deja de pisar el modelo del agente: es el modelo por defecto del negocio.
9. **Panel.** El flujo, los guardrails, el JSON crudo y las versiones pasan a la ficha del agente. «AgentSpec y proveedor» se convierte en «Cuenta y proveedor». El flujo deja de mostrarse en Configuración.
10. **Sin migración de data real.** Test y producción se resiembran (D-68). Una migración siembra `agent_drafts.flow` desde el `flow.json` del negocio para los agentes existentes y las releases anteriores siguen leyendo `flow_tenant`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener el flujo compuesto por negocio (ADR-0054/0109) | El dueño configura lo mismo en dos lugares y la ficha del agente no dice la verdad; dos agentes del mismo tipo no pueden diferir (D-72) |
| Flujo por tipo de agente, heredado en vivo | Dos agentes del mismo tipo no pueden diferir y el dueño no puede ajustar el suyo; el dueño eligió lo contrario |
| Guardar el flujo en `AgentSpec.extensions` | `extensions` es spec versionable y el contrato v3 rechaza campos desconocidos; el flujo es operativo y editable, como los playbooks (ADR-0026) |
| Solo procedimientos, sin documento de flujo | El editor de etapas (HU-AOS-45) aún no existe y el dueño necesita editar secciones y acciones ahora; los procedimientos abiertos se siguen derivando del flujo |
| Una tabla `agent_flows` aparte | Duplica el ciclo de vida del borrador y la versión; las columnas en `agent_drafts`/`agent_versions` lo reutilizan |

## Consecuencias

**Positivas:** el modelo por agente queda completo; dos agentes del mismo tipo pueden vender distinto; Configuración deja de duplicar; el flujo se congela con la versión y la release; el editor de superpoderes tiene una base editable.

**Negativas / lo que aceptamos pagar:** toca releases y runtime; el manifiesto por agente crece; dos agentes del mismo tipo mantienen dos flujos (mitigado con la semilla y «copiar del pack»); hay que mantener la lectura de `flow_tenant` mientras existan releases viejas.

**Qué haría que revisáramos esto:** que los dueños no entiendan por qué dos agentes del mismo tipo tienen flujos separados, que el tamaño del manifiesto o la compilación por agente degraden el turno, o que el flujo y los procedimientos diverjan en producción de forma recurrente (→ unificar en procedimientos).

## Impacto en el código

- `core/db/models.py` (`AgentDraftRecord`, `AgentVersionRecord`) y migración del core con `flow`, `flow_hash` y la semilla.
- `core/agents/versiones.py`, `core/api/routers/agent_editor.py` (flujo en borrador, versión y publicación) y `docs/openapi.json`.
- `core/releases/build.py`, `core/releases/cargar.py`, `core/releases/manifiesto.py`, `core/releases/diff_legible.py`.
- `core/agents/compiler.py`, `core/orchestrator/loop.py`, `core/agents/flow.py` (resolución por agente y semilla).
- Panel: `lib/agentes-editor.ts`, `app/(app)/agentes/[id]/editar/*`, `lib/configuracion-secciones.ts`, `app/(app)/configuracion/_components/advanced-panel.tsx` y `settings-tabs.tsx`.
- Tests: flujo y versión, release con dos agentes, compatibilidad con `flow_tenant`, aislamiento cross-tenant, panel y accesibilidad.
