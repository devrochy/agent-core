# ADR-0191 — Un solo motor: retiro del runtime `legacy`

- **Estado:** propuesto
- **Fecha:** 2026-11-12

## Contexto
D-60 (2026-11-03) dejó el Agent OS v3 como único motor y `legacy` solo como rollback por consola; D-69
(2026-11-12) aprueba la parte irreversible antes del salto a producción con base nueva (D-59, HU-AOS-69).
En develop `a49c08c3` el runtime v2 sigue vivo: el loop corre el `AgentSpec` v2 de `agent_specs` cuando
no hay release (negocio `legacy`, conexión del Centro de pruebas y negocio v3 sin release), la
bandera global `AOS_CONNECTION_ROUTING` vale `False` por defecto, el gate de evals del CI corre sin
release y el build v3 sigue refrescando el General desde v2 (`refrescar_desde_v2`).

## Decisión
Todo turno corre sobre una release v3 de su conexión; sin release no hay respuesta (falla cerrada con
alerta). El runtime v2, el lector tolerante y la bandera global se borran del código.

Detalle:
1. **Release del turno siempre.** El loop sale antes de cargar el spec v2 si la conexión no tiene
   release activa. `exige_concesion()` queda verdadero en todo turno (cierra el hueco que dejaba el
   Centro de pruebas en HU-ARCH-34).
2. **Release 1 al alta de la conexión.** Crear o activar una conexión conversacional construye y
   activa su release 1 (cierra lo pendiente de la adenda HU-AOS-15 de ADR-0117). Sin eso, el punto 1
   dejaría mudas las conexiones nuevas.
3. **Centro de pruebas por una conexión real.** La conversación de prueba se ata a una conexión
   conversacional del negocio (la por defecto, o la elegida, como en HU-AOS-57) y usa su release. La
   conexión interna `playground` sale del turno; si el negocio no tiene ninguna conexión con release,
   el panel lo dice y no responde.
4. **Configuración deja de editar agentes** (decisión del dueño, 2026-11-13). Identidad, Voz y Cerebro
   quedan como datos del negocio (nombre, descripción, persona base en `agent_specs`) y solo alimentan
   el contenido inicial de un agente nuevo; la conducta de cada agente se edita en su ficha. Así se
   puede borrar `refrescar_desde_v2` sin que Configuración deje de hacer algo en silencio.
5. **Se borran:** las ramas `aos_runtime = 'legacy'` del loop, consumer y kernel; `core/agents/tolerante.py`
   (sus constantes y utilidades vigentes se mueven a un módulo propio); `refrescar_desde_v2` y su uso en
   `build.py` y `release_uno.py`; el `403 aos_runtime_legacy`; el `Literal["legacy","v3"]` de la API;
   `agent-core db migrar-a-v3 --volver`; y `AOS_CONNECTION_ROUTING` (el ruteo por conexión es siempre).
6. **Evals.** Los journeys y el golden siembran sus negocios directo en `agentspec/3` con release. La
   línea base «sin motor» pasa a ser «v3 sin procedimientos». Las casetes se regraban una vez con LLM
   real (OK del dueño del 2026-11-12) y se mide pass^4 antes y después.
7. **Base.** En la misma tanda, una migración hace que un negocio nazca `('v3', false, 'off')` y que
   `aos_fijar_banderas` rechace `legacy`. La columna `tenants.aos_runtime` y sus CHECK se sueltan en un PR
   posterior, ya desplegado este. Que no queden negocios en `legacy` se comprueba con un comando antes
   del despliegue, no con un precheck dentro de la migración.

**Fuera de esta decisión:** `tenants.aos_procedures`, `tenants.aos_routing`, el motor y el ruteo en sombra
son funciones de v3 con su despliegue por etapas (D-60 D3) y siguen por negocio. El General se retira
en ADR-0192. Se quedan `agent_specs`/`AgentSpecRecord` como almacén de la configuración del negocio
(la unión de habilidades de D-68 vive ahí), la vista interna `spec_de_release`,
`agent_versions.legacy_agent_spec_id` y el `Lane "legacy"` de la lectura de decisiones históricas.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Mantener `legacy` como rollback por consola (D-60 tal cual) | Dos motores que probar en la base nueva de producción; con la bandera apagada por defecto un negocio nuevo nacería en `legacy` |
| Volver constantes también `aos_procedures` y `aos_routing` | Cambia lo que ven hoy los clientes de los negocios en `false/off`; su encendido es por etapas y se mide aparte |
| Que el Centro de pruebas use la release de otra conexión | Rompe la FK compuesta `(tenant_id, connection_id, release_id)` de ADR-0117 §2 |
| Construir una release para la conexión interna `playground` | Mete gate, canary y ciclo de vida en una conexión sin otro caso de uso |
| Respuesta con el spec v2 si falta la release | Es justo el camino que se retira; mantiene un turno sin concesiones (HU-ARCH-34) |

## Consecuencias
**Positivas:** un solo camino que probar, auditar y operar; toda respuesta sale de una release
inmutable con sus concesiones; la base nueva de producción no puede nacer en `legacy`.

**Negativas / lo que aceptamos pagar:** sin vuelta a `legacy`: el rollback es la release anterior de v3
(ADR-0117) o el respaldo de la base. Un negocio sin conexión con release no se puede probar en el
Centro de pruebas. Se regraban las casetes del gate (costo de LLM, una vez). Las HU del Centro de
pruebas y del alta cambian de forma.

**Qué haría que revisáramos esto:** un incidente P1 en producción en el que la release anterior de v3 y
el respaldo no basten para recuperar el servicio en menos de una hora, o que más del 5 % de las altas de
conexión de un mes queden sin release activa.

## Impacto en el código
`core/orchestrator/{loop,consumer,ruta_de_conversacion}.py`, `core/kernel/{release_del_turno,banderas,oferta,probar_frase}.py`,
`core/kernel/procedures/esperas.py`, `core/shared/release_vigente.py`, `core/releases/{build,release_uno,migracion_v3,contrato,canary,canary_job,reporte_bindings,corredor}.py`,
`core/agents/{tolerante,backfill_agentes,versiones,instancias}.py`, `core/presets/{__init__,agentes,apply}.py`, `core/alta_negocio.py`,
`core/admin/agent_os.py`, `cli/commands/db.py`, `core/api/routers/{agents,a2a,connection_releases,skill_installations,connection_agents,connections,platform,settings,playground}.py`,
`core/config.py`, `core/config_registro/*`, `core/qa/{datos_aos,escenarios}.py`, `evals/journeys.py`, `evals/gate_release.py`,
`scripts/gate_evals.sh`, una migración nueva (alta en v3 y setter sin `legacy`) y `docs/openapi.json`.

Estado de ADR anteriores (nota, no edición): ADR-0113 (el lector tolerante «hasta retirar v2» se retira
aquí), ADR-0117 §2 «Migración» y la adenda HU-AOS-15 (release 1 desde el tolerante y `release_missing`
legacy), ADR-0112 (orden de banderas: queda sin `runtime`), ADR-0139 §3 (sin la bandera global) y
ADR-0075 (el Centro de pruebas prueba por una conexión real).

## Adenda (2026-11-14): la voz de Configuración se siembra al publicar

**Caso.** En el onboarding el dueño elige tono, calidez y emojis antes de crear su primer agente, pero
la release nacía con la voz que el Coordinador tenía del alta (los valores por defecto). El agente
quedaba creado con otra voz y el cambio no se evidenciaba; además, `tono.md` no interpolaba `{tone}`,
`{warmth}` ni `{emojis}`, así que esas tres opciones no llegaban al prompt.

**Decisión.**
- El playbook `core/playbooks/tono.md` usa las variables de persona y explica cada nivel (tono, calidez
  0–5 y emojis). La voz deja de ser texto fijo.
- `core/agents/base.py::sembrar_voz_del_negocio(tenant_id)` compara la persona viva de `agent_specs` con
  el `voice` del último `KernelSpec` del Coordinador y, si difiere, publica una versión nueva del
  Coordinador (idempotente por hash, conserva routing/procedimientos/políticas).
- Se llama antes de construir la release en `core/agents/editor.py::publicar_en_conexiones` (onboarding,
  alta normal y publicación de agentes) y en `core/api/routers/connection_releases.py::build_release`
  (build manual). No se llama en `release_uno.py` ni en `reconstruir` de referencias retiradas: esos
  caminos reproducen releases históricas.
- Una release ya activa no cambia en caliente: la voz nueva entra en la próxima versión publicada, como
  el resto de la configuración congelada. La UI de Configuración › Voz y estilo lo dice.

**Consecuencias.** El primer agente del onboarding hereda la voz elegida; editar la voz después no toca
lo que ya está atendiendo hasta publicar una versión nueva. Cambiar `tono.md` invalida las grabaciones
del gate y obliga a regrabarlas (aprobado por el dueño el 2026-11-14).

**Código:** `core/playbooks/tono.md`, `core/llm/prompt_builder.py`,
`core/agents/base.py`, `core/agents/editor.py`, `core/api/routers/connection_releases.py`,
`tests/test_prompt_fecha_actual.py` y `tests/test_voz_del_negocio.py`. En el panel y la APK, la calidez
pasa a 0–5 con seis etiquetas y el preview distingue cada nivel.
