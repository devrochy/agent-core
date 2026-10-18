# ADR-0139 — Presets v2: agentes especialistas desde plantillas y reglas del Coordinador

- **Estado:** propuesto (2026-10-18, HU-AOS-46; decisiones revisables por el dueño en el PR)
- **Fecha:** 2026-10-18
- **Contexto:** HU-AOS-46 (SPEC-26), adelantada de R3 por la decisión D-35 («SolversHub vende agentes»).
  Enmienda ADR-0110 §1 y §3 y ADR-0118 §1 (dónde viven las plantillas de agente). Usa ADR-0112 (banderas),
  ADR-0113 (`agentspec/3`, `kernelspec/1`), ADR-0117 (releases) y la adenda de HU-AOS-16 en ADR-0118.

## Contexto

- Un preset (ADR-0110) habilita packs en el AgentSpec v2. ADR-0113 saca `packs` del contrato v3 y ningún documento
  decía cómo un preset crea agentes del Agent OS.
- Hoy cada negocio v3 corre el Coordinador y el General (lector tolerante). El dueño pidió, en su informe de
  pruebas, agentes «para un fin» (Ventas, Cobranza, Agenda, Suscripciones) por negocio y conexión.
- El enrutador que lee `routing.rules` es de HU-AOS-17 (en paralelo). El contrato `kernelspec/1` ya fija la forma
  de una regla (`{id, when, agent|start}`, DSL sin regex).

## Decisión

### 1. `PresetManifest` v2

El manifiesto gana un campo opcional `agentes: [<key de plantilla>]`. Sin `agentes` el manifiesto es v1 y el negocio
sigue solo con el General. Los cuatro presets existentes pasan a v2 y suben su `version` (ADR-0110 §1):

| Preset | Agentes |
|---|---|
| `comercio` (v2) | Ventas |
| `restaurante` (v2) | Ventas |
| `cobranza` (v2) | Cobranza |
| `suscripciones` (v2) | Ventas, Agenda, Suscripciones |

### 2. Plantillas de agente en el core (enmienda ADR-0118 §1)

Una plantilla es **configuración del core** (`core/presets/plantillas/<key>.json`, modelo `PlantillaAgente` con
`extra="forbid"`), como el manifiesto del preset: `key`, `nombre`, `descripcion` (metadato del agente, no entra al
prompt), `habilidades` (packs obligatorios), `opcionales` (packs que se suman si están habilitados), `procedimientos`
(ids `procedure/1` de esas habilidades), `playbooks` (nombres) y `reglas` (`{id: "preset_…", when}` con el
`RoutingWhen` de `kernelspec/1`). Nunca lleva prompts (invariante 3).

| Plantilla | Habilidades | Opcionales | Procedimientos | Playbooks |
|---|---|---|---|---|
| `ventas` | pack_commerce | pack_payments | tomar_pedido, contratar_plan | ventas, seguimiento, pagos |
| `agenda` | pack_calendar | — | agendar_cita | citas |
| `cobranza` | pack_cobranza | — | promesa_de_pago | cobranza, seguimiento-cobranza |
| `suscripciones` | pack_subscriptions | — | cambiar_plan | suscripciones, planes |

`pack_payments` es opcional en Ventas porque el alta lo deja pendiente de claves (ADR-0110 §3.2): si fuera
obligatorio, casi ningún comercio nuevo tendría Ventas.

**Enmienda a ADR-0118 §1:** las plantillas de agente **no** entran por ahora en `pack_versions`
(`manifiesto_de_pack` sigue con `agent_templates: []`, sin cambiar el hash de ningún pack). Moverlas a los packs
(una plantilla por habilidad, versionada con su contenido) queda para la parte 2 de HU-AOS-16; mientras tanto la
plantilla solo se usa al crear el agente, y la versión del agente (inmutable) es lo que entra en la release.

### 3. Qué crea `aplicar_agentes` (`core/presets/agentes.py`)

Solo con `AOS_CONNECTION_ROUTING` encendida y `tenants.aos_runtime = 'v3'` (ADR-0112). Con cualquiera apagada no
escribe nada y, con la global apagada, ni siquiera lee la base: la conducta de los negocios legacy y el gate en
replay no cambian. Precondiciones: Coordinador y General publicados (backfill de HU-AOS-13) y al menos una conexión
conversacional con Coordinador vivo.

Por plantilla cuyas `habilidades` estén habilitadas en el negocio (`enabled_business_packs`, la única lectura de la
habilitación hasta la parte 2 de HU-AOS-16):

- **Agente `specialist`** con la `key` de la plantilla y su **versión 1**, con contenido `agentspec/3` **derivado
  de la última versión del General**: modelo, persona, memoria (`writeNamespaces`) y políticas del General;
  `procedures` = los de la plantilla publicados y ofrecibles, con rango `^M.0`; `tools` = los bindings del General
  de las acciones del core o de sus habilidades; `guardrails` del General (un `required_tool` solo si su acción
  sigue); playbooks del General de la plantilla o del core; sin `declarativeTools` (siguen en el General).
- **Instancia** en cada conexión conversacional con Coordinador cuyas habilidades efectivas (ADR-0118 §2) cubren las
  de la plantilla, **nunca por defecto**: el General sigue atendiendo hasta que HU-AOS-17 enrute (si el
  especialista fuera el `is_default`, el foco cambiaría ya, adenda de HU-AOS-16 en ADR-0117).
- **D-35, un agente publicado tiene al menos una conexión:** sin ninguna conexión con sus habilidades no se crea; si
  uno recién creado no entra en ninguna (un conflicto), se archiva y queda anotado para reintentarlo.

### 4. Reglas del Coordinador

El Coordinador es **un agente por negocio** y su KernelSpec entra en la release de todas las conexiones. Por eso
solo se agregan las reglas de los agentes presentes en **todas** las conexiones conversacionales: una release nunca
lleva una regla hacia un agente que no es componente suyo. Se agregan al final de `routing.rules`
(`{id, when, agent: <key>}`) y se publica una versión nueva del Coordinador; nunca se cambian ni se quitan reglas,
ni `defaultAgent` (sigue `general`). Tope del contrato: 50 reglas. Si alguien publicó el Coordinador mientras se
aplicaba, no se publica encima (control optimista) y la corrida se reporta con error.

Formato y semántica para HU-AOS-17: es el contrato `kernelspec/1` sin extensiones. Las plantillas usan solo
`keywordAny` (frases de datos, no prompts). La semántica de coincidencia (normalización, palabra completa) es de
HU-AOS-17; si decide otra cosa, las reglas `preset_*` se reescriben con una versión nueva de la plantilla.
**Pendiente de acordar con HU-AOS-17:** el General de la release declara también los procedimientos de los
especialistas (lector tolerante), así que el router desempata entre ambos; y la unificación de `is_default` con
`defaultAgent`.

### 5. Release candidata, sin activarla

Por cada conexión que cambió (instancia nueva o Coordinador nuevo) se construye la release candidata
(`core/releases/build.py::construir`) y **no se promueve** (ADR-0117 §2: la release no se activa sola). Un build que
falla se reporta por conexión y no frena las demás. La promoción la decide el dueño con la API de releases.

### 6. Idempotencia: no duplica ni pisa lo que el dueño cambió

`tenants.settings["preset"]["agentes"]` (JSONB existente, sin migración) acumula lo que hizo el preset: `creados`
(keys que creó), `archivados` (los que archivó él por D-35) y `reglas` (ids que agregó alguna vez), más el resumen de
la última corrida; `audit_log` registra `preset_agentes_aplicados`. Volver a aplicar:

- no crea un agente cuya key ya existe; solo **adopta** (instancia, regla) los que creó el preset (`key_ocupada` si
  lo armó el dueño); uno borrado o archivado por el dueño no vuelve;
- nunca publica sobre la versión de un especialista: lo que el dueño publicó queda;
- no agrega una instancia en una conexión que ya la tuvo, viva o quitada por el dueño;
- no agrega una regla que ya está ni una que el preset agregó y el dueño borró (lápida);
- no construye release si nada cambió.

### 7. Entradas

- `apply_preset` (ADR-0110 §3) llama a `aplicar_agentes` al final, en el mismo `tenant_scope`. Un error se registra y
  el preset sigue aplicado (el alta no falla); se reintenta por la CLI.
- **Camino real para negocios nuevos y existentes:** el alta deja el negocio en `legacy` y sin agentes base, así que
  ahí el paso es un no-op. El camino es: `backfill-agentes` → `backfill-releases` → `migrar-a-v3` →
  `agent-core db presets-v2 --tenant <slug> [--preset <id>] [--aplicar]` (dry-run por defecto; `--todos` recorre
  los negocios v3 con su preset registrado; `--preset` solo con `--tenant`).

### 8. Lo que no cubre (reasignación de deudas que decían «hasta HU-AOS-46»)

- **Permisos de colaboración** (`a2a_graphs`, `agent_links`): no se escriben; no hay runtime de colaboración ni la
  release congela grafos. Pasan a HU-AOS-18. La HU queda parcial.
- **Presets y alta escriben un AgentSpec v2** (ADR-0113 §5 y adenda HU-AOS-12): siguen así para habilitar packs
  hasta la parte 2 de HU-AOS-16 (`pack_installations` de scope negocio).
- **Limpiar los literales de secretos de `agent_specs`** (adenda HU-AOS-13 de ADR-0113): mientras el loop legacy los
  lea no se pueden borrar; pasa al retiro de v2 (HU-AOS-15 parte 2 o posterior).
- **Retirar la vista v2 inversa** (`core/agents/spec_de_release.py`, adenda HU-AOS-15 de ADR-0117): pasa a la HU que
  haga que el loop lea `agentspec/3` directo; esta HU no toca el loop.
- **Aplicar a una sola conexión** (Gherkin «Instalar»): se instala en todas las conexiones que tienen las
  habilidades; un `--conexion` llega con el panel (HU-AOS-23).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Plantillas dentro de cada pack (`pack_versions`) | Cambia el hash de los cinco packs y el lock; el build y el runtime no las leen todavía. Queda como parte 2 de HU-AOS-16 |
| Especialista como agente por defecto | Cambia el foco de la conversación antes de que exista el enrutador (HU-AOS-17) |
| Reglas aunque el agente falte en alguna conexión | Una release con una regla hacia un agente ausente: semántica indefinida para el router |
| Diferir todas las reglas a HU-AOS-17 | El pedido del dueño incluye las reglas; con el formato del contrato ya fijado, escribirlas es seguro y AOS-17 decide cómo leerlas |
| Promover la release del preset | ADR-0117: la release no se activa sola; el dueño decide |

## Consecuencias

**Positivas:** un negocio v3 queda con sus agentes «para un fin», configurables por el dueño, en una release lista
para promover; reaplicar es seguro; los negocios legacy no cambian.

**Negativas / lo que aceptamos pagar:** dos fuentes de contenido de un agente (plantilla en el core y, más adelante,
en el pack); el especialista sale de la última versión del General **antes** del refresco que hace el build (el
build en v3 refresca solo el General); con `migrar-a-v3 --volver` el build en legacy refresca el Coordinador desde el
v2 y las reglas del preset se pierden (al volver a v3, `presets-v2` no las repone porque están en la lápida: hay que
quitarlas del registro a mano); tras instalar especialistas, la release activa deja de estar «al día» para
`migrar-a-v3 --dry-run` hasta promover la candidata.

**Qué haría que revisáramos esto:** que HU-AOS-17 defina reglas de otra forma; que las plantillas cambien más
seguido que los releases del core; que un negocio necesite agentes distintos por conexión.

## Impacto en el código

- `core/presets/__init__.py` (`PresetManifest.agentes`, `PlantillaAgente`), `core/presets/plantillas/*.json`,
  `core/presets/agentes.py` (nuevo), `core/presets/apply.py` (enganche), `core/presets/<id>.json` (v2).
- `cli/commands/db.py` (`presets-v2`).
- Tests: `tests/test_presets_v2.py`, `tests/integration/test_presets_v2_agentes.py`.
