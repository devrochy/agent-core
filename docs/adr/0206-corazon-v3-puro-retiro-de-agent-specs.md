# ADR-0206 — Corazón V3 puro: el turno corre sobre AgentSpecV3 y KernelSpec, y se retiran `agent_specs` y el `AgentSpec` v2

- **Estado:** aceptado (2026-11-21, por decisión del dueño; documento mergeado en agent-core#860, plan en solvershub-docs#282). Sin código todavía; el código va en HU-AOS-90 y HU-AOS-91, después de [ADR-0205](0205-coordinador-receptor-y-lead-base.md).
- **Fecha:** 2026-11-20
- **Contexto:** [D-78](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-78--corazón-v3-puro-coordinador-receptor-con-captura-de-lead-base-y-manifiesto-pack--habilidad--tools), [SPEC-50](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/spec.md) (REQ-AOS-29), [HU-AOS-90](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/hu/HU-AOS-90-loop-nativo-agentspecv3-y-kernelspec.md) y [HU-AOS-91](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/hu/HU-AOS-91-retiro-agent-specs-v2-y-stubs.md); absorbe HU-REP-01.
- **Enmienda:** [ADR-0191](0191-un-solo-motor-retiro-del-runtime-legacy.md) («Fuera de esta decisión»: `agent_specs`, `AgentSpecRecord` y la vista interna `spec_de_release` se quedaban; §4: los datos del negocio viven en `agent_specs`) y [ADR-0190](0190-habilidades-por-agente.md) (la unión de habilidades del negocio en `agent_specs.packs`).
- **Usa:** invariantes 1 (multi-tenant con RLS), 3 (cero prompts en código), 4 (contratos solo por ADR: este ADR retira `AgentSpec` v2 de `agentcore_contracts`) y 8 (sin borrado físico de filas de negocio); ADR-0117 (caché por versión), ADR-0118 §2 (`pack_installations` como única fuente de habilitación, con scopes negocio, conexión y agente), ADR-0190 §2 (instalación por agente).

## Contexto
En `origin/develop` 0f9ab7f3 el turno corre sobre un `AgentSpec` v2 armado en memoria:
`spec_v2_desde_release` (`core/agents/spec_de_release.py`) convierte el `AgentSpecV3` del agente en foco y el
`KernelSpec` del Coordinador. La llama `core/agents/compiler.py` una vez por versión (es el camino normal), el
respaldo `spec_del_turno` y las consultas A2A de `loop.py`. Además el loop llama a `load_active_spec()` en cada
turno, que puede escribir porque siembra el spec por defecto. En el runtime también leen `agent_specs`
`core/tools/core_skill.py` (playbooks y presupuesto), `core/releases/build.py` (packs y allowlist de playbooks) y
`core/kernel/ingress.py` (texto de escalamiento), y en tiempo de turno también `core/agents/playbooks.py` (la
allowlist en cada carga), `core/datos_cliente/tools.py`, `core/memory/historial_tool.py`,
`core/subscriptions/tools.py` y `core/kernel/procedures/registro.py`; `core/agents/base.py` siembra el `KernelSpec`
desde `agent_specs`. En total, `load_active_spec` aparece en ≈ 46–49 archivos de `core/` y `packages/` (≈ 81 llamadas
sin contar imports; ≈ 145 con imports) y `leer_spec_activo` en ≈ 10–11. Casi todas esas lecturas son para saber qué packs tiene el negocio.

## Decisión
El turno lee el `AgentSpecV3` del agente en foco y el `KernelSpec` del Coordinador directo de la release
congelada, y nada del core ni de los packs lee o escribe `agent_specs`.

Detalle:
1. **Loop.** `fijar_release_del_turno` entrega el agente en foco como `AgentSpecV3` (o la vista de recepción de
   ADR-0205 §5 si el foco es el Coordinador) y el `KernelSpec`. `loop.py` y `opciones_toque.py` dejan de llamar a
   `load_active_spec()`. `prompt_builder`, los guardrails, las declarativas, el compilador de caché y A2A reciben
   esos modelos tipados: los guardrails son `AgentSpecV3.guardrails` y las declarativas
   `AgentSpecV3.declarativeTools`, no `spec.extensions`. Se borra `core/agents/spec_de_release.py`.
2. **Declarativas con secretos.** Los `SecretRef` se siguen resolviendo en la ejecución, nunca desde la release.
   Hoy pasan las declarativas con secretos en headers y se omiten las que los tienen en URL o body; este ADR no
   cambia eso. Soportar secretos en URL o body es trabajo nuevo y va en su propia HU cuando un cliente lo pida.
3. **Caché por versión.** El compilador arma los bloques fijos desde `AgentSpecV3` y `KernelSpec.voice`, con la
   misma clave de versión que hoy. `usage_ledger.agent_spec_version` conserva el formato
   `{agent_key}@v3:{version}`.
4. **Lo que el runtime leía del v2.** El presupuesto por conversación sale del `KernelSpec`. Para los playbooks
   manda el agente: los que declara su `AgentSpecV3` (`PlaybookRefV3`), y el manifiesto de la release solo valida
   que existan en sus packs; no hay una segunda lista. El texto de
   escalamiento sale del playbook del core, como cualquier respuesta (invariante 3).
5. **Packs activos del negocio.** Se cumple lo que ADR-0118 §2 ya decidió: `pack_installations` es la única
   fuente. Una función async del core, `packs_activos_del_negocio(tenant_id)`, devuelve la unión de las
   instalaciones activas de scope negocio y de agentes no archivados. Su resultado tiene la misma forma que
   `enabled_packs` (incluye `core`), para que los crons de los packs, la cuota del plan, el dashboard,
   `lead_registry`, `datos_cliente`, el enrutador de webhooks de pago y las rutas del panel migren sin cambiar su
   lógica. La versión sin `core` reemplaza a `enabled_business_packs`. Las instalaciones de scope conexión
   reemplazan enteras a las del negocio, como en ADR-0118 §2. El build las valida contra
   `packs_activos_del_negocio`, no contra `agent_specs`, así que `instalacion_sin_habilitar` significa «sin
   instalación activa en el negocio ni en sus agentes».
   **Precheck** antes de desplegar HU-AOS-90: un comando por negocio lista los packs que están en `agent_specs.packs`
   y no tienen instalación activa, porque al cambiar de fuente esos packs se perderían sin aviso. Se despliega
   con cero diferencias o con cada diferencia resuelta.
6. **Datos del negocio.** Nombre, descripción, voz y modelo por defecto del negocio (lo que ADR-0191 §4 dejaba en
   `agent_specs`) viven en `AppSettings`; solo la voz se siembra en `KernelSpec.voice` (el `KernelSpec` no gana campos). Solo alimentan el
   contenido inicial de un agente nuevo, como hoy.
7. **Escrituras.** Lo que hoy escribe en `agent_specs` pasa a su dueño: la habilitación de packs a
   `pack_installations` (ADR-0190 §2), y las declarativas y guardrails de las rutas del panel al `AgentSpecV3`
   del agente, como borrador versionado (HU-FWK-09). `presets/apply.py` deja de guardar un spec.
8. **Contrato.** `AgentSpec` v2 y los tipos que solo usa él salen de `agentcore_contracts`, que pasa a 4.0.0. Se
   quedan los que usan `KernelSpec`, `AgentSpecV3` o el core (por ejemplo `Tone`, `Emojis` y `SideEffect`). Se borran
   `core/agents/spec.py` y `core/agents/declarativas.py` (v2), si ningún import lo impide. `AgentSpecV3`,
   `KernelSpec` y `SideEffect` siguen igual.
9. **Tabla.** Una migración borra `agent_specs` y la columna `agent_versions.legacy_agent_spec_id` en HU-AOS-91
   (decisión del dueño del 2026-11-20). Va en un PR posterior al que deja de leerla, ya desplegado. Antes de
   correrla se saca un respaldo de **cada entorno donde exista la tabla**. Si para entonces producción ya tiene
   negocios reales, un precheck confirma que nada sin respaldo depende de esas filas. Retirar por ADR un esquema
   que ningún código lee no es el borrado físico de filas de negocio que prohíbe la invariante 8; se registra
   explícitamente aquí.
10. **Residuos.** Se borran los paquetes stub `apps/{agentos,gateway,worker}` y
    `packages/{core-crm,core-runtime,channel-whatsapp}`. `channel-whatsapp` se instala hoy por el glob
    `packages/channel-*/` del `Dockerfile` y de `scripts/install_plugins.sh`; el glob queda (sigue instalando
    `channel-telegram`, `channel-twilio`, `channel-web` y `channel-waweb`) y solo deja de encontrar ese paquete
    vacío. Antes se verifica que nada importe `agentcore_channel_whatsapp`. `CLAUDE.md` se actualiza en el mismo PR.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Mantener `spec_v2_desde_release` como adaptador interno (ADR-0191 tal cual) | El turno sigue corriendo sobre un contrato que nadie edita; las declarativas con secretos se pierden y cada campo nuevo de V3 hay que mapearlo dos veces |
| Dejar `agent_specs` como almacén comodín y solo cortar el loop | Dos fuentes para los packs del negocio (`agent_specs.packs` y `pack_installations`) que pueden divergir, con la cuota del plan leyendo la vieja |
| Deprecar la tabla y borrarla en una HU posterior | El dueño eligió borrarla en HU-AOS-91: los datos son de prueba y una tabla muerta invita a leerla de nuevo |
| Un `AgentSpecV3` sintético para el Coordinador guardado en `agents` | El Coordinador quedaría editable como un agente más, en contra de ADR-0205 §5 |

## Consecuencias
**Positivas:**
- Un solo contrato real en el turno, sin traducción.
- La fuente de los packs del negocio es una sola y coincide con lo que el runtime ofrece.
- Las declarativas con secretos funcionan en el turno.

**Negativas / lo que aceptamos pagar:**
- Refactor amplio: unos 46 archivos de runtime, panel y packs, y 44 archivos de test.
- Contrato mayor (4.0.0), con regeneración de `openapi.json` si alguna respuesta exponía el v2.
- Los packs que hoy llaman a `load_active_spec` (commerce, calendar, cobranza, subscriptions y payments) suben
  versión en `packages/pack-versions.lock.json`.
- La migración que borra la tabla es irreversible; se cubre con el respaldo y con el despliegue en dos PR.

**Qué haría que revisáramos esto:** que el golden de los 4 tipos de agente baje de `pass^4 = 100 %` tras HU-AOS-90,
o que el hit de caché de prompts por versión caiga más de 5 puntos frente a la línea base.

## Impacto en el código
- `core/orchestrator/loop.py`, `core/orchestrator/opciones_toque.py`, `core/agents/compiler.py`,
  `core/llm/prompt_builder.py`, los guardrails del orchestrator, `core/mcp/declarative.py`, `core/kernel/ingress.py`,
  `core/tools/core_skill.py`, `core/releases/build.py` (`_efectivas`), `core/agents/playbooks.py`,
  `core/agents/base.py`, `core/datos_cliente/tools.py`, `core/memory/historial_tool.py`,
  `core/subscriptions/tools.py`, `core/kernel/procedures/registro.py` y `core/billing/enrutador.py`.
- Borrados: `core/agents/spec_de_release.py`, `core/agents/spec.py`, `core/agents/declarativas.py`,
  `AgentSpecRecord`, `AgentSpec` v2 de `packages/contracts`, los stubs de `apps/` y `packages/`.
- Nueva función `packs_activos_del_negocio` (en `core/agents/instalaciones_agente.py`) y la migración de cada
  lector de `enabled_packs(await load_active_spec())`.
- Migraciones: retiro de `agent_specs` y de `agent_versions.legacy_agent_spec_id`.
