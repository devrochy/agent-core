# ADR-0185 — Informar planes es una consulta, no una puerta: `sub_listar_planes` sale de las acciones solo de procedimiento

- **Estado:** aceptado (2026-11-03, OK del dueño a la opción (a), a ofrecerla solo con el motor activo y a regrabar la cassette del gate; implementado en este PR salvo `MONEY_KEYS`, ver «Alcance»)
- **Fecha:** 2026-11-03
- **Contexto:** re-medición pass^4 del 2026-11-03 (corrida #14 de `evals-procedimientos`, develop `ac66a190`) y su causa raíz (PR #677, `causa-raiz-pass4-corrida-14`); SPEC-43 / D-55 (PR-3 de agent-core, changelog del 2026-10-30).
- **Enmienda:** ADR-0113, adenda HU-AOS-08 («Acciones solo de procedimiento») en lo que toca a `sub_listar_planes`; la lista de tools del PR-3 de ADR-0177. No cambia ningún contrato, esquema ni migración.
- **Usa:** ADR-0113 (adenda 2026-10-12: «las consultas y lo que no se confirma con el cliente se siguen ofreciendo»), ADR-0119 (verificador), ADR-0177.

## Contexto

Desde el PR-3 de SPEC-43 las cuatro tools de inscripción de `pack_subscriptions` (`sub_listar_planes`, `sub_cotizar_inscripcion`, `sub_confirmar_inscripcion`, `sub_inscribir`) están en `SOLO_PROCEDIMIENTO` (`core/subscriptions/tools.py`): llevan `solo_con_motor_activo`, ningún `flow.json` las nombra y `get_tools()` no las marca `always_available`. Con el motor activo y **sin tarea en foco**, `ContextoMotor.herramientas()` ofrece solo lo que el turno ya traía (`_base`) menos las puertas; `sub_listar_planes` no está en `_base`, así que no se ofrece. Con foco, solo aparece dentro de las etapas de `contratar_plan` que la nombran.

El PR-4 retiró además `informar_plan` de commerce (ADR-0184, contexto), la única consulta de planes que existía sin tarea.

Dos hechos medidos:

1. **El caso que hoy "pasa" es un rodeo.** La grabación `evals/cassettes/journeys/suscripciones/01-informar-planes.json` (gate de CI, `JOURNEYS="01,05"`) muestra a un cliente que solo pregunta «¿qué planes tienen y cuánto cuestan?»: el modelo llama `load_playbook(contratar_plan)` → `iniciar_procedimiento(contratar_plan)` → `sub_listar_planes` (tres vueltas en vez de una), deja **una tarea de compra abierta** para quien no quiere comprar y cierra empujándolo: «Para ayudarte a contratar, cuéntame: 1. Plan». Depende de que el modelo adivine un camino que nada le indica (el log de la corrida #14 tiene `playbook_not_allowed playbook=contratar_plan`: adivina un playbook con el id del procedimiento).
2. **Cuando no adivina, escala.** Corrida #14, `agendar-una-demo` con motor: el cliente simulado abrió preguntando por el Plan Premium; sin foco la tool no existe, `planes.md` manda usarla, el modelo llamó `sub_consultar` + `load_playbook` y escaló antes de pedir la demo (3/4).

La regla vigente ya decía otra cosa: la adenda del 2026-10-12 de ADR-0113 reserva solo las **puertas** (lo que compromete y se confirma con el cliente) y deja ofrecidas «las consultas y lo que no se confirma» (`informar_plan`, `consultar_inventario`, `check_availability`, `sub_consultar`…). `solo_con_motor_activo` se definió para «una acción que solo existe para un procedimiento». `sub_listar_planes` es de solo lectura (`solo_lectura=True`), no crea nada y se limita al catálogo del negocio; meterla en `SOLO_PROCEDIMIENTO` fue una decisión del PR-3, no de ADR-0177 (que dejaba las tools `sub_*` «fuera de este PR»).

Un hallazgo aparte, que queda como seguimiento (ver decisión 3): **el precio que devuelve `sub_listar_planes` no cuenta como evidencia para el verificador.** `_plan_para_el_modelo` entrega el precio bajo la clave `precio`, y `MONEY_KEYS` (`core/shared/dinero.py`) no la incluye (sí `precio_lista`, `precio_periodo_siguiente`, `valor_periodo`…). `extract_monetary_values` devuelve `[]` para ese resultado. Comprobado con el verificador real: con el resultado de `sub_listar_planes`, «El Plan Basic cuesta $200.000 al mes.» da `monto_sin_evidencia` y «…200.000 COP…» pasa solo porque `MONEY_IN_TEXT` solo detecta los montos con `$` o `COP` **delante** (`\$\s?\d…|\bCOP\s?\d…`). El journey grabado se salvó por esa redacción; un cliente real, con el modelo escribiendo «$200.000», hoy se bloquea y escala, incluso dentro de `contratar_plan`.

## Decisión

1. **`sub_listar_planes` es una consulta del core.** Sale de `SOLO_PROCEDIMIENTO`; con eso `get_tools()` la marca `always_available` y entra a `_base`, así que se ofrece con el motor activo, con o sin tarea en foco. Sigue en las etapas `elegir_plan`, `cotizar` y `datos` de `contratar_plan` (el procedimiento no cambia: se queda en 2.0.0). Las otras tres (`sub_cotizar_inscripcion`, `sub_confirmar_inscripcion`, `sub_inscribir`) siguen solo de procedimiento: cotizar ya presupone una compra (fija el `monto_a_pagar` que el cliente confirma con botón y mira al cliente de la conversación y su código de promoción), y una pregunta del tipo «¿cuánto me costaría con el código X?» es intención de compra, que ya enruta a `contratar_plan`.
2. **Con el motor activo, no sin él (decisión del dueño, 2026-11-03).** Conserva `solo_con_motor_activo(predicate)`. Con la bandera apagada el prompt de cada negocio queda idéntico (promesa de ADR-0113) y no se tocan cassettes legacy; además, sin motor un negocio de suscripciones no puede inscribir (el procedimiento no existe), así que ofrecer solo el listado lo dejaría informando lo que no puede vender. Si más adelante se quiere que un negocio sin motor pueda informar planes, es cambiar el predicado a `_proveedor_habilitado()` a secas y regrabar las cassettes legacy que tengan `pack_subscriptions`.
3. **El catálogo es la única fuente.** El resultado sigue saliendo de `capability.listar_planes` / `plan_por_codigo` (tabla de planes del negocio, RLS forzada), nunca de la ficha ni del playbook. Los **límites** (`entitlements`) no son dinero y el verificador no los compara: la garantía es que el modelo solo los cita de lo que la tool devolvió (regla del playbook, abajo). **Seguimiento separado (el dueño no lo autorizó en este PR):** agregar `precio` a `MONEY_KEYS` para que el verificador respalde el precio del listado con la redacción habitual. Hoy `precio` solo lo emite `_plan_para_el_modelo`, así que el cambio no abriría el verificador a otras tools, y no altera el prompt ni las cassettes. **Hasta entonces, un «$200.000» tomado del listado se bloquea con `monto_sin_evidencia`** (y «200.000 COP» pasa), con lo que el cliente puede recibir un escalamiento en vez del precio.
4. **Conducta en el playbook, no en código** (invariante 3). `packages/pack-subscriptions/playbooks/planes.md`, sección «Informar planes»: para preguntas informativas (qué planes hay, precios, qué incluyen, si hay prueba) llamar `sub_listar_planes` directamente, **no** iniciar `contratar_plan` solo para informar; contratar es cuando el cliente dice que quiere el plan; nunca dar precio, límite o prueba que la tool no devolvió; sin planes activos (`encontrado: false`) decir que por ahora no hay planes publicados, sin inventar. La descripción de la tool dice lo mismo («úsala cuando el cliente pregunte qué planes hay, cuánto cuestan o qué incluyen; no inicia la contratación»).
5. **Aislamiento y alcance por release.** No hay camino de datos nuevo: la misma capability y la misma tabla con RLS. La tool es de dueño `core`, así que `permitida_para_el_agente` la deja pasar siempre y `candidatas_del_spec` la suma sola a las `siempre` del General (le resta `SOLO_PROCEDIMIENTO`): las versiones de agente y las releases ya publicadas la ofrecen sin republicar ni migrar. Con foco en **otro** procedimiento se ofrece como el resto de las tools del core de solo lectura (`sub_consultar`); es lectura sin efectos y se acepta.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| **(a) Tool de solo lectura fuera del procedimiento (elegida)** | Una sola vuelta, sin tarea ni botón, y ya es la regla de ADR-0113 para las consultas. El costo es el que se paga abajo: el conjunto de tools del turno cambia y hay que regrabar. |
| (b) Procedimiento corto de consulta de planes | Compite en el ruteo con `contratar_plan` (los dos hablan de planes; el golden de ruteo tendría que separar «¿qué incluye el Premium?» de «quiero el Premium», con riesgo de precisión en lo que más importa, la compra), abre tarea y episodio para una lectura sin pasos, confirmación ni cierre, y cuesta una vuelta de `iniciar_procedimiento`. Los procedimientos son para lo que compromete. |
| (c1) Responder desde la ficha / el playbook / el catálogo estático | Viola la invariante pedida: precios y límites no salen de la ficha ni de la memoria; se desactualiza al editar un plan en el panel y el verificador no tiene evidencia (los textos del prompt no respaldan montos). |
| (c2) Tarjeta de planes inyectada en el bloque dinámico del prompt | Evidencia del servidor sin tool, pero paga tokens en cada turno aunque nadie pregunte, cambia el prompt de todas las conversaciones del negocio, y el verificador no toma montos del contexto (habría que ampliarlo). Se reconsidera si la consulta de planes resulta frecuente. |
| (d) Que `contratar_plan` arranque con preguntas informativas (ampliar `triggers`) | Es lo que hoy hace el modelo por su cuenta, formalizado: abre tareas de compra de quien no compra, ensucia las métricas de embudo y empuja a comprar. Además baja la precisión del ruteo (falsos positivos donde el golden pide «ninguno»). |

## Consecuencias

**Positivas:** el cliente que solo pregunta recibe la respuesta en una vuelta y sin tarea abierta; deja de depender de que el modelo adivine; todo lo que compromete sigue detrás de procedimiento y botón.

**Negativas / lo que aceptamos pagar:**

- **El conjunto de tools de un turno sin foco con el motor activo cambia** (lleva `sub_listar_planes`). La grabación `01-informar-planes` se hizo con el conjunto anterior (hash de `herramientas` `a58e89fe…`) y el casete incluye ese hash en su clave: con el cambio, `bash scripts/gate_evals.sh replay` falla cerrado en ese journey («herramientas agregadas: sub_listar_planes»; el golden del gate sigue en verde) hasta regrabarlo con LLM real (~0,01 USD, OK del dueño dado).
- La conducta se apoya en el playbook y en la descripción de la tool; si el modelo igual rodea por `contratar_plan`, hay que medirlo (pass^4) y, si hace falta, endurecerlo.
- Un negocio sin motor sigue sin poder informar planes (punto 2).
- Mientras `precio` no sea clave de dinero (seguimiento de la decisión 3), la respuesta con «$» delante puede bloquearse.

**Qué haría que revisáramos esto:** que con la tool el modelo siga abriendo `contratar_plan` para preguntas informativas (se vería en el journey nuevo y en `reporte-ruteo`), o que se decida que un negocio sin motor debe poder informar.

## Alcance de este PR y lo que queda

Hecho: `sub_listar_planes` fuera de `SOLO_PROCEDIMIENTO` (con la bandera del motor), descripción de la tool, sección «Informar planes» de `planes.md`, pruebas unitarias e integración (oferta con y sin motor, sin foco, sin tarea abierta, aislamiento entre negocios, bindings del General) y la regrabación de `suscripciones/01-informar-planes`.

Queda fuera, con OK pendiente o decisión del dueño:

- **`precio` en `MONEY_KEYS`** (decisión 3): seguimiento separado.
- **Journey `procedimientos/05-informar-planes-sin-comprar`** y los casos «ninguno» de planes en `evals/ruteo/golden.jsonl`: correr el journey es LLM real fuera de lo autorizado y, sin correrlo, no se sabe si ya pasa; sin el cambio fallaría por diseño y ensuciaría la medición nocturna. Descritos en HU-SUB-37.
- **Medición pass^4** con `evals-procedimientos` (1–3 USD): pendiente de OK.

## Impacto en el código

- `core/subscriptions/tools.py`: `sub_listar_planes` fuera de `SOLO_PROCEDIMIENTO`; descripción de la tool.
- `packages/pack-subscriptions/playbooks/planes.md`: sección «Informar planes».
- Pruebas: `tests/test_subscriptions_tools_inscripcion.py`, `tests/test_procedimientos_de_packs.py`, `tests/integration/test_procedimientos_agenda_subs_cobranza.py`.
- Evals: `evals/cassettes/journeys/suscripciones/01-informar-planes.json` regrabado.

## Verificación

Replay verde con la cassette regrabada; pruebas de oferta (motor activo sin foco: `sub_listar_planes` sí, cotizar/confirmar/inscribir no; motor apagado: ninguna) y de aislamiento entre negocios. Pendientes con OK: pass^4 de `evals-procedimientos` sin bajar frente a la corrida #14 y el journey nuevo 4/4 sin llamar `iniciar_procedimiento`; `reporte-ruteo` con las preguntas informativas en «ninguno»; con el seguimiento de `precio`, `$200.000` pasa con el listado del turno y `$250.000` inventado se bloquea.
