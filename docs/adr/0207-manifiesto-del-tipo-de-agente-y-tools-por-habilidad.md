# ADR-0207 — El pack declara su tipo de agente y cada habilidad declara sus tools

- **Estado:** aceptado (2026-11-21, por decisión del dueño; documento mergeado en agent-core#860, plan en solvershub-docs#282). §1–§4 implementados en HU-AOS-92 (2026-11-23); §5 va en HU-AOS-93 y §6 en HU-AOS-94.
- **Fecha:** 2026-11-20
- **Contexto:** [D-78](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-78--corazón-v3-puro-coordinador-receptor-con-captura-de-lead-base-y-manifiesto-pack--habilidad--tools), [SPEC-50](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/spec.md) (REQ-AOS-29), [HU-AOS-92](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/hu/HU-AOS-92-manifiesto-de-tipo-y-tools-por-habilidad.md), [HU-AOS-93](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/hu/HU-AOS-93-datos-cliente-vs-modulo-en-especialistas.md) y [HU-AOS-94](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/hu/HU-AOS-94-api-catalogo-tipos-habilidades-y-tools.md); reemplaza la mudanza pendiente de HU-AOS-64.
- **Enmienda:** [ADR-0190](0190-habilidades-por-agente.md) §1 (el pack de especialidad define el tipo de agente: ahora lo declara en un manifiesto propio, no en `core/presets/plantillas/*.json`), §8 (grupos de acciones) y §9 (API del agente).
- **Usa:** invariantes 2 (sin fork por cliente), 3 (cero prompts en código) y 5 (un pack no importa de otro); ADR-0117 §4 y ADR-0118 §1 y §4 (manifiesto de acciones con su efecto, pendiente de HU-AOS-34: este ADR lo cumple), ADR-0155 §8 (etiquetas en el panel), ADR-0198 (datos del cliente por agente), ADR-0203 (dominios de superpoderes).

## Contexto
En `origin/develop` 0f9ab7f3 el tipo de agente (Ventas, Agenda, Cobranza y Suscripciones) se declara en las 4
plantillas `core/presets/plantillas/*.json`, que leen el editor, `agent_editor.py`, `instalaciones_agente.py`,
`presets/agentes.py`, `requisitos.py`, `guion.py` y `qa/datos_generados.py`. Los datos de una tool están
repartidos:
- `MCPTool` declara `scopes`, `side_effects`, `solo_lectura` y `sensible`;
- `BaseModule.grupos_de_acciones` declara el grupo;
- `ActionInfo` (`core/kernel/procedures/catalogo.py`) junta nombre, dueño, `side_effect` y scopes para los
  procedimientos;
- el manifiesto `pack/2` (`core/releases/packs.py`) lista acciones, playbooks, secciones del flow y procedimientos.

`BaseSkill` agrupa tools sin exponerlas por habilidad, y ninguna API dice qué tools trae cada habilidad.

## Decisión
Cada pack de especialidad declara en su `BaseModule` el manifiesto de su tipo de agente, y el host arma un único
catálogo `Habilidad → Tools` a partir de lo que ya declaran las tools.

Detalle:
1. **Manifiesto del tipo.** `BaseModule.tipo_de_agente: AgentTypeManifest | None` (solo en packs `especialidad`)
   con `key`, `nombre`, `descripcion`, `seccion`, `habilidades_obligatorias`, `habilidades_opcionales`,
   `procedimientos_por_defecto`, `playbooks_por_defecto`, `flujo_por_defecto` y todo lo que hoy traen las
   plantillas (`preguntas`, `preguntas_retiradas`, `reglas`, `opcionales`, `acciones_opcionales`, `captar_leads` y
   `datos_de_prueba`). Las habilidades se nombran por id (`"pack_payments"`, `"core:leads"`): ningún pack importa a
   otro (invariante 5). El manifiesto es un JSON versionado dentro del pack (como los fragmentos de presets), no
   una constante de Python, porque lleva textos de UI. Las 4 plantillas de `core/presets/plantillas/` se retiran
   y `load_plantilla` lee del pack, con la misma forma que hoy para no cambiar a sus lectores en el mismo PR.
2. **Catálogo de tools sin duplicar metadatos.** `ToolCatalogItem` es una vista de solo lectura que arma el host,
   no un segundo lugar donde declarar. Lleva:
   - `name`, `description` y `scopes`, de `MCPTool`;
   - `group`, de `grupos_de_acciones`;
   - `side_effect` (`read` / `write` / `irreversible`, el `SideEffect` del contrato), declarado a mano en cada
     tool con un test que lo exige. No se infiere de `MCPTool.side_effects`, que significa otra cosa (el canal
     `test` no ejecuta el handler, HU-ARCH-14): `remember_fact` y `update_customer_profile` lo tienen en `True`;
   - `requires_confirmation_default`.

   Extiende `ActionInfo`, que pasa a ser el mismo tipo, para que procedimientos, catálogo y API lean una sola fuente.
3. **Habilidades del core.** `core:datos_cliente`, `core:operacion` y `core:leads` se declaran con la misma forma
   (`SkillCatalogItem`: `id`, `nombre`, `descripcion` y `tools`) en un registro del core. Las del Coordinador
   (ADR-0205 §5) se exponen como una habilidad fija `core:recepcion`, de solo lectura.
4. **Validación.** Al arrancar y en un test de contrato, el host falla si:
   - una tool de un pack no tiene grupo o `side_effect`;
   - un procedimiento por defecto referencia una tool que no está en las habilidades de su tipo;
   - un tipo declara una habilidad opcional inexistente;
   - dos habilidades declaran la misma tool (hoy `catalogo.py` lo ignora con un `setdefault`; pasa a fallar).
5. **Datos del cliente vs datos del módulo.** El manifiesto del tipo nombra las operaciones de datos del cliente
   de su `data_policy` (ADR-0198) por separado de las tools del módulo. `pack_cobranza` suma su `datos.py`
   (`cobranza.promesa_de_pago`), como ya hacen los otros tres. El especialista enriquece la fila base del lead
   que asegura el core (ADR-0205 §7); no crea una segunda.
6. **API.**
   - `GET /admin/agent-types` devuelve el Coordinador y los 4 tipos desde los manifiestos, y reemplaza a
     `GET /admin/agent-templates`; `POST /admin/agent-types/{key}/agents` reemplaza a
     `POST /admin/agent-templates/{key}/agents`, la ruta de creación. Las dos viejas quedan como alias hasta que
     panel y APK migren, y las retira otra HU.
   - `GET /admin/skill-catalog` devuelve cada habilidad con sus `ToolCatalogItem`, sin secretos ni configuración.
     No usa `/admin/skills` para no compartir prefijo con `/admin/skills/installations`, que sí depende de lo
     instalado.
   - `GET /admin/agents/{id}/posibles` devuelve, para un agente, su habilidad de especialidad, la de datos del
     cliente y las transversales compatibles con su tipo, con su estado (`lista`, `falta_configurar` o
     `sin_agregar`) y sus tools.

   Complementa `GET`/`PUT /admin/agents/{id}/skills` y `GET /admin/agent-superpowers`, que siguen. El
   `PUT /admin/agents/{id}/skills/{pack_id}` responde 409 `habilidad_incompatible` a una transversal que el
   tipo no admite o a un pack de especialidad ajeno. Las tres rutas nuevas exigen el permiso de lectura de la
   sección Agentes y corren en `tenant_scope`. Sus clientes son la ficha del agente y la estación «Habilidades»
   del panel y la APK (HU-AOS-95): `/posibles` para la ficha, `skill-catalog` para el detalle de cada habilidad.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Dejar el tipo en `core/presets/plantillas/*.json` | El core sabe de los tipos de cada pack; un pack nuevo exige tocar el core (va contra el espíritu de la invariante 2) |
| `ToolCatalogItem` declarado a mano en cada pack | Repite scopes, grupo y efecto que ya están en `MCPTool` y `grupos_de_acciones`, y se desincroniza |
| Exponer el catálogo dentro de `/admin/plugins` | `/admin/plugins` es por negocio; el catálogo es por tipo de agente y no depende de lo instalado |
| Rutas nuevas que reemplacen también `/admin/agents/{id}/skills` | Rompe el panel y la APK durante el despliegue sin ganar nada: el `PUT` sigue siendo la forma de instalar |

## Consecuencias
**Positivas:**
- Un pack nuevo trae su tipo y sus tools sin tocar el core.
- El panel muestra exactamente qué hace cada habilidad.
- Procedimientos, catálogo y API leen una sola fuente de metadatos.

**Negativas / lo que aceptamos pagar:**
- Los 7 packs suben versión en `packages/pack-versions.lock.json`.
- **Cambia una conducta:** `tuvo_efecto_irreversible` (`core/kernel/release_del_turno.py`) trata
  `side_effect = None` como irreversible, y hoy todas las acciones lo tienen así. Al declararlo, una tarea que solo
  leyó o escribió algo reversible se cancela en vez de escalarse cuando cambia la release. Los tests de cambio de
  release lo cubren en HU-AOS-92.
- Las tools sin `side_effect` claro hay que clasificarlas a mano una vez.
- `/admin/agent-templates` convivió como alias hasta que panel y APK migraron; se retiró el 2026-11-26 (ver la adenda).

**Qué haría que revisáramos esto:** que un pack nuevo necesite declarar algo de su tipo que el manifiesto no
admite y obligue a tocar el core, o que la validación de arranque bloquee un despliegue por una tool de un tenant.

## Impacto en el código
- `core/mcp/types.py` (`AgentTypeManifest`, `SkillCatalogItem`, `ToolCatalogItem`/`ActionInfo`),
  `core/kernel/procedures/catalogo.py`, `core/releases/packs.py` y `core/plugin_manager.py` (validación).
- `core/presets/__init__.py` (`load_plantilla` desde el pack); se retira `core/presets/plantillas/`.
- `packages/pack-{commerce,calendar,cobranza,subscriptions}` (manifiesto del tipo) y
  `packages/pack-{payments,knowledge,voice}` (catálogo); `packages/pack-cobranza/.../datos.py`.
- `core/api/routers/agent_editor.py` (rutas nuevas y 409), `docs/openapi.json` y los tipos del panel y la APK.

## Adenda HU-AOS-92 (2026-11-23): cómo quedó
- El manifiesto vive en `packages/pack-<x>/pack_<x>/tipo_de_agente.json`. `load_plantilla` lo lee de los archivos de
  `packages/`, no de los módulos cargados, para que no dependa de qué escaneó cada proceso (API, worker, CLI). El
  arranque lo valida con los módulos cargados. `flujo_por_defecto` se declara y se valida contra el `flow.json`; lo
  consumen HU-AOS-94 y HU-AOS-95.
- `requires_confirmation_default` sale de `MCPTool.sensible`, no es un campo nuevo.
- Clasificación del efecto: `irreversible` = mueve dinero, envía algo fuera (correo, mensaje, aviso a una persona) o
  cancela; `write` = escribe algo reversible en la base del negocio. Generar un link de pago (`sub_link_pago`,
  `confirmar_pago_pedido`) o inscribir tras una etapa confirmada (`sub_inscribir`) es `write`: el dinero lo mueve el
  cliente en la pasarela. Si fueran irreversibles, los procedimientos `tomar_pedido`, `pago_en_linea` y
  `contratar_plan` dejarían de pasar el linter. Como las releases ya publicadas se vuelven a lintear en cada
  despliegue (`registro._valido`), los agentes de Ventas y Suscripciones perderían esos recorridos.
- Los procedimientos abiertos (`0.0.0`) que se derivan de los `flow.json` ahora sí ven irreversibles sin confirmar
  (por ejemplo `cancel_calendar_event` o `escalate_to_human`). No cambia nada en V3: solo resuelven nombres y nunca
  se ofrecen en un turno.
- Las tools de datos del cliente, del aviso de datos y del historial entran al catálogo de habilidades
  (`core:datos_cliente`), pero no al catálogo del linter: así no cambia qué acciones admite un procedimiento.
- La validación de arranque solo mira el core y los packs de la plataforma (`es_de_plataforma`). Un plugin de un
  negocio nunca frena un despliegue.
- Los comandos del hilo (`core/kernel/procedures/comandos.py`) no están en `core:operacion`: se arman por
  procedimiento en cada turno y no caben en un catálogo estático. Igual declaran `side_effect = "read"`.
- `validar_catalogo` también falla si una tool del core (salvo las `sub_*`, que despacha un pack) no está en ninguna
  habilidad `core:*`. Así una tool nueva del core no queda fuera del catálogo sin aviso.
- Un tipo puede nombrar habilidades `core:*`, pero `load_plantilla` las saca de `PlantillaAgente`. Esa forma solo
  lleva packs, que son lo que se habilita e instala, y todo agente ya trae las del core.
- La regla de los procedimientos por defecto revisa `actions` y `preconditions`, y acepta cualquier tool cuyo dueño
  en runtime sea el core, también las `sub_*`. `pack_commerce.pago_en_linea` (Ventas) usa `sub_link_pago` sin
  `requires`, como ya lo admite el linter.
- Al criterio de `irreversible` se suman los puntos donde el cliente compromete algo o donde se registra dinero
  recibido: `confirmar_cotizacion`, `sub_confirmar_inscripcion` y `cob_registrar_abono`. Al cambiar la release, una
  tarea que pasó por uno de ellos se escala a una persona y no se cancela en silencio.
- Si un pack de la plataforma no carga (por ejemplo `pack_payments`, opcional de Ventas), la API no arranca, porque
  el tipo nombra una habilidad que no existe. Es lo buscado: el despliegue se queda en la versión anterior en vez de
  arrancar degradado sin avisar.

## Adenda HU-AOS-94 (2026-11-25): API del catálogo y de los tipos

- **Campo nuevo `habilidades_transversales` en el manifiesto.** Lista las transversales que el tipo admite
  (`pack_knowledge`, `pack_payments`, `pack_voice`). Gobierna solo la compatibilidad y el catálogo: no entra en los
  requisitos del asistente de creación, que siguen saliendo de `habilidades_opcionales`. `validar_catalogo` exige que
  cada una exista y sea `tipo = "transversal"`, y que toda transversal de la plataforma la declare algún tipo.
- **Rutas.** `GET /admin/agent-types` (superconjunto de `agent-templates`, con el Coordinador aparte),
  `GET /admin/skill-catalog`, `GET /admin/agents/{id}/posibles` y `POST /admin/agent-types/{key}/agents` (alias de la
  creación por plantilla). Las rutas `agent-templates` quedan `deprecated` y con la misma salida.
- **Permisos.** Las lecturas nuevas piden la sección `agents`; crear sigue pidiendo `settings:write`. El panel debe
  tratar 403 en `/agent-types` como «sin permiso».
- **Tipo del agente.** Sale de su `source_template` y, si no, de su `key`. Un agente sin tipo recibe 200 con listas
  vacías y `type_key = null`; el Coordinador (`kind = core`) recibe 200 con `type_key = "coordinador"`, su habilidad fija
  `core:recepcion` y sin transversales.
- **409 `habilidad_incompatible`** en `PUT /admin/agents/{id}/skills/{pack}` al instalar una transversal que el tipo no
  admite o un pack de especialidad ajeno. Se comprueba antes de escribir y solo en la instalación nueva: configurar lo
  ya instalado y quitar nunca dan 409. Un agente sin tipo no se restringe.

## Adenda 2026-11-26: retiro de `agent-templates`

Panel y APK ya leen `GET /admin/agent-types` y crean con `POST /admin/agent-types/{key}/agents`; la OTA de la APK
se publicó antes. Se quitan `GET /admin/agent-templates` y `POST /admin/agent-templates/{key}/agents`, y con ellas
los esquemas `AgentTemplate` y `Page_AgentTemplate_` del contrato. `agent-types` no cambia: lleva todo lo que traía
la plantilla. Quien siga llamando a las rutas viejas recibe 404 (una APK sin la OTA); `agent-types` pide la sección
`agents` y la creación sigue pidiendo `settings:write`.
