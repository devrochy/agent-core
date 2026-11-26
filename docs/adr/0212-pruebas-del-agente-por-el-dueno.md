# ADR-0212 — Pruebas del agente por el dueño: escenarios, corridas en el sandbox, juez y cobro en créditos

- **Estado:** aceptado (2026-11-26, por decisión del dueño; k con valor por defecto y tope editables incluido). Sin código todavía; el código va en HU-AOS-100 a HU-AOS-105.
- **Fecha:** 2026-11-26
- **Contexto:** D-81 y SPEC-53 (`solvershub-docs`), HU-AOS-99.
  **Usa:** ADR-0117 y su adenda del gate (`core/releases/veredicto.py`, `kind='eval'`), HU-ARCH-14 (sandbox de tools,
  `core/mcp/sandbox.py`, purga `0131`), HU-AOS-72 (prueba del borrador, `core/releases/efimera.py`), ADR-0146
  (`core/ia/estructurado.py`, `kind='generacion'`), ADR-0208 y ADR-0209 (crédito único, `alcanza()`, saldo) y el
  tarifario `v2` (`core/billing/tarifario.py`).
  **No cambia:** el gate de release ni el contrato `AgentSpecV3` (invariante 4 de `CLAUDE.md`).

## Contexto

Las pruebas automáticas de hoy son de la plataforma:

- El gate de release (`core/releases/corredor.py`, `evals/gate_release.py`) corre journeys escritos a mano en la base `agentcore_eval`, los juzga con aserciones sobre tablas y lo paga la plataforma (`kind='eval'`, `credits=0`, tope `EVAL_TOPE_USD_MENSUAL`).
- El dueño solo puede probar a mano en el playground, un mensaje a la vez.

D-81 pide que el dueño pueda pedir a la IA un set de escenarios de su negocio, aprobarlos, correrlos sin efectos reales, ver qué falla y aplicar un arreglo, pagando en créditos después de una cuota gratis y siempre con estimado y aprobación.

Tres hechos del código condicionan el diseño:

1. **La purga del playground borra el ledger.** `purge_test_conversation` (migración `0131`) borra físicamente la conversación `test`, sus mensajes y su `usage_ledger`. El gasto del playground solo se mide mientras la conversación vive (`_de_playground` en `core/billing/creditos.py:82`) y la cuota de 300 créditos se calcula sobre eso, sin contador. Una corrida cobrada no puede apoyarse en filas que se purgan.
2. **El playground tiene una conversación viva por conexión o por borrador** (`get_or_create_test_conversation`), y `close_test_conversation` sin argumentos cierra todas las `panel` del negocio. Una corrida que reutilice o comparta esas conversaciones se pisa con el dueño que prueba a mano.
3. **No hay juez.** Los journeys deciden pass/fail con aserciones deterministas sobre la base (`evals/journeys.py`). Un escenario escrito por la IA o por el dueño no puede traer aserciones de tablas, y el sandbox no escribe en ellas.

## Decisión

**El dueño tiene escenarios por agente. Una corrida los ejecuta uno a uno en el sandbox del playground con un cliente simulado, un juez decide si pasan, y el costo se cobra en créditos después de una cuota gratis, con estimado y aprobación previa. Es un módulo aparte del gate: el gate sigue igual y lo sigue pagando la plataforma.**

### 1. Datos

Migración `0160_pruebas_del_agente` (el número final sigue el orden de merge). Tres tablas con `tenant_id NOT NULL`, RLS forzada con la policy `nullif` del repo, `created_at`/`deleted_at` y borrado lógico (`purge_tenant` descubre las tablas con `tenant_id` y las cubre sin cambios):

- `agent_test_cases`: `agent_id`, `titulo`, `objetivo_cliente`, `datos_cliente` jsonb, `criterios` text[], `tools_esperadas` text[], `tools_prohibidas` text[], `origen` (`ia`|`manual`), `estado` (`borrador`|`aprobado`), `created_by`. Máx. 50 por agente, validado en la aplicación, y un índice único parcial `(tenant_id, agent_id, lower(titulo)) WHERE deleted_at IS NULL`.
- `agent_test_runs`: `agent_id`, `estado` (`en_cola`|`corriendo`|`terminada`|`cancelada`|`cortada_por_tope`|`fallo`), `k`, `version_probada` jsonb (borrador o release activa, con su hash), `creditos_estimados`, `creditos_de_cuota`, `creditos_cobrados`, `tope_creditos`, `pedida_por`, tiempos.
- `agent_test_results`: `run_id`, `case_id`, `intento`, `estado` (`pasa`|`falla`|`no_se_pudo_probar`), `fin` (`objetivo`|`se_rindio`|`tope`), `transcripcion` jsonb, `tools_llamadas` text[], `criterio_incumplido`, `razon`, `arreglo` jsonb, `arreglo_aplicado_en`, `juez_equivocado` bool.

`ck_usage_kind` suma `prueba_agente`. Los escenarios y resultados guardan la transcripción en estas tablas, **no** en `messages`: la conversación de la corrida se purga al terminar el escenario.

### 2. Dónde corre: el sandbox, no la base de eval

Cada escenario corre en una conversación de prueba **propia de la corrida**, del canal `test`, creada por `crear_conversacion_de_corrida` en `core/pruebas/` (no con `get_or_create_test_conversation`, que reutiliza la viva), con `state = {panel: true, prueba_corrida: <run_id>, prueba_caso: <case_id>}` y, si se prueba el borrador, también `draft_agent_id` (`CLAVE_BORRADOR`, `core/releases/efimera.py:38`), que `core/kernel/release_del_turno.py` valida en cada turno. Reglas:

- **Sandbox por defecto.** `is_panel_test` es verdadero (`state["panel"]`), así que `core/mcp/host.py::ejecutar_tool` llama al simulador de cada tool con efectos y falla cerrado (`herramienta_sin_simulador`) si no lo tiene. Un escenario que llega a una tool así queda `no_se_pudo_probar`, no `falla`, y no gasta juez.
- **Versión probada.** Si el agente tiene borrador, se prueba el borrador con la release efímera (como HU-AOS-72); si no, la release activa de su conexión. Se guarda en `version_probada`.
- **Cliente sintético propio.** Cada corrida crea su propio `customer` sintético (`panel-prueba-<run_id>`). `purge_test_conversation` borra el cliente `panel-playground` cuando no le quedan conversaciones y corre con `session_replication_role = replica` (sin FK): compartirlo con el dueño o con otra corrida (concurrencia 2) deja una conversación huérfana.
- **No se pisa con el dueño.** Las conversaciones con `prueba_corrida` se excluyen de **todas** las ramas de `close_test_conversation` (sin argumentos, por `connection_id` y por `agent_id`), de `reset_test_conversation` y de `core/tasks/playground_cleanup.py`. Esa tarea purga las de una corrida muerta con un umbral propio (más de 1 hora); no se agrega otro barrido. La corrida purga cada conversación al terminar su escenario.
- **Turnos sin saldo.** Los turnos de la corrida pasan por `DecisionDelTurno` (colchón o `agotado`, `core/billing/creditos.py`). Un turno `agotado` no es una falla del agente: el escenario queda `no_se_pudo_probar` y la corrida corta con `cortada_por_saldo`.
- **No usa `agentcore_eval`.** La base de eval y su corredor de una sola corrida para toda la plataforma (`CANDADO` en `gate_release.py`) quedan solo para el gate.

### 3. Cliente simulado y juez

- **Cliente.** `ClienteSimulado(objetivo, datos, estilo, semilla)` con el `objetivo_cliente` y los `datos_cliente` del escenario. Termina con `[OBJETIVO_CUMPLIDO]` o `[ME_RINDO]`; el tope de **12 turnos** lo aplica el corredor (el simulador no lo tiene). **Se mueve a `core/pruebas/simulador.py`** con su prompt `cliente.md` como playbook del core: `pyproject.toml` empaqueta solo `core*` y `cli*`, y `core/` no importa de `evals/`. `evals/simulador.py` pasa a reexportarlo. El objetivo y los datos del escenario son del dueño y entran al prompt delimitados como **dato** (invariante 7), no concatenados con `.format()` al texto de instrucciones.
- **Chequeo determinista primero.** Con la lista de tools llamadas (que sale del registro del sandbox): si el agente llamó una `tools_prohibidas`, falla; si nunca llamó una `tools_esperadas`, falla. En ambos casos **no se llama al juez LLM** y el escenario ahorra su costo.
- **Juez LLM después.** `core/pruebas/juez.py` recibe la transcripción (en un bloque delimitado como dato), las tools llamadas y los `criterios`, y devuelve con `generar_estructurado` `{pasa, criterio_incumplido, razon}`; una salida que no valida el esquema se descarta y el escenario queda `no_se_pudo_probar`. **Los prompts del juez, del arreglo y del simulador viven como playbooks del core** (`load_core_playbook`, como el resto de `generar_estructurado`), no en `.py` (invariante 3). El juez usa un **modelo fijo de la plataforma**, independiente del nivel de IA del agente (ADR-0210), para que el veredicto no cambie si el dueño sube o baja de nivel. El prompt del juez dice que ante la duda no falla y exige citar el turno.
- **El juez puede equivocarse.** El resultado muestra la razón y el dueño puede marcarlo `juez_equivocado`; ese resultado sale del porcentaje y se mide (objetivo: menos del 10 %).
- **k (repeticiones por escenario) con valor por defecto y tope editables.**
  - **Por defecto:** k = 1, de `PRUEBAS_K_POR_DEFECTO` (config de plataforma).
  - **Tope:** el dueño elige k entre 1 y `PRUEBAS_K_MAXIMO` (4 al empezar), que es de plataforma y se cambia sin desplegar con la configuración del negocio `tenants.settings.pruebas.k_maximo` (solo el master) para bajarlo o subirlo por negocio, sin pasar nunca del techo global. El dueño también puede guardar su k habitual en `tenants.settings.pruebas.k_por_defecto`, dentro de ese tope.
  - **Qué hace k:** con k > 1 un escenario pasa si pasan todos los intentos (como `pass^k` del gate) y se marca inestable si difieren. Cada intento corre con su propia conversación, cliente simulado y juez, así que el costo crece casi lineal con k.
  - **Fuera de rango:** un k mayor al tope responde 422 `k_fuera_de_rango`.

### 4. Cobro

Hay dos tipos de filas del ledger de una corrida, y se tratan distinto:

- **Filas que sobreviven.** El juez, el arreglo y la propuesta de escenarios salen por `generar_estructurado` (`core/ia/estructurado.py`), que ya escribe su propia fila con `conversation_id = NULL` y los créditos calculados por `creditos_por_evento`, sin pasar por ninguna conversación. Ese `kind` se pasa por parámetro: `prueba_agente` para el juez y el arreglo, `generacion` (ADR-0146) para proponer escenarios. **No se vuelven a sumar a ninguna fila consolidada** (si no, se cobrarían dos veces).
- **Filas que se purgan.** Los turnos del agente (`turn`, `agent_run`, etc.) cuelgan de la conversación de prueba y `purge_test_conversation` las borra. Antes de purgar, dentro de la misma transacción, el corredor suma sus créditos y USD reales, suma el costo del cliente simulado (que llama al proveedor directo y no escribe nada) y escribe **una** fila `kind='prueba_agente'` con `conversation_id = NULL`, `agent_instance_id` del agente probado y los créditos **brutos**.

**Un solo cubo para la cuota gratis.** `ck_usage_kind` suma `prueba_agente`, y `core/billing/creditos.py::medir_creditos` pasa a tratar todas las filas `prueba_agente` como del cubo del playground, con una sola fórmula:

`cobrado = max(0, playground_vivo + pruebas_del_ciclo − PLAYGROUND_GRATIS_POR_CICLO)`

- `playground_vivo` es lo de siempre, pero `_de_playground` **excluye** las conversaciones con `prueba_corrida` (si no, durante la corrida se contarían dos veces, vivas y luego consolidadas). Lo mismo para el filtro de plantillas por `Message`.
- `pruebas_del_ciclo` es la suma de los créditos brutos de las filas `prueba_agente` del ciclo.
- La cuota **no se guarda en ninguna tabla**: es derivada, así que no hay carrera entre el dueño probando a mano y una corrida. El estimado usa la misma función: `de_la_cuota = min(estimado, max(0, 300 − usado_hasta_ahora))`. `agent_test_runs.creditos_de_cuota` queda como dato informativo del estimado y no entra en ningún cálculo de saldo.
- En el desglose, ese cubo se muestra con el nombre «Pruebas y playground». Los consumidores de `por_concepto` (`plan_view`, panel y APK) se revisan en HU-AOS-100. El gasto por agente que ya existe agrega el cubo en el renglón sin agente; se acepta.
- `core/billing/tipos_de_uso.py` suma `prueba_agente` a `NO_ES_ACTIVIDAD` y `NO_SON_TURNOS`. A diferencia de `eval`, sí cuenta como gasto del negocio (por el cubo).

**Estimado y aprobación.**
- **Estimado.** Es una función nueva, `core/billing/pruebas.py::estimar_corrida` (no una extensión de `cotizar`, que vive en `core/billing/creditos.py:294` y solo cubre `vision`). Calcula por escenario: turnos esperados (6; techo 12) × créditos medios de un turno en el nivel de IA del agente + cliente simulado + juez, y se calibra con la medición real de HU-AOS-105 (los 11,1 y 45,1 créditos de HU-BILL-29 son solo el punto de partida). Devuelve la tabla **por cada k permitido** (de 1 al tope): `por_k: [{k, estimado, de_la_cuota, del_saldo, saldo_despues}]`, más el saldo y la cuota gratis restante, para que el panel muestre cuánto cuesta cada k antes de elegirlo. El estimado de un k es el de k = 1 por escenario × k (turnos del agente, cliente simulado y juez se repiten por intento). El cliente ve créditos y su equivalente en conversaciones; el master ve además el costo real en USD, que no sale de la respuesta del gestor.
- **Aprobación.** `POST .../corridas` recibe `k` y exige `confirmo_creditos` igual al estimado de ese k (si cambió, 409 `estimado_cambio`) y corre `alcanza()` sobre `del_saldo` antes de crear la corrida (409 `plan_limit_creditos`). Es el patrón de `core/qa/importar_fotos.py`.
- **Proponer escenarios** cobra `generacion` desde el saldo, con `alcanza` antes de gastar y el costo visible en el panel antes de pedirlo.

**Topes y planes.**
- `tope_creditos` = estimado del k elegido × 1,5, que el corredor comprueba después de cada escenario; si lo pasó, corta con `cortada_por_tope` y cobra solo lo gastado. Cancelar cobra lo gastado.
- Máx. 20 escenarios por corrida, una corrida a la vez por negocio (409 `corrida_en_curso`, con candado consultivo) y 5 corridas por día.
- **Con llave propia (BYOK):** el agente corre con el modelo de la llave del negocio y esos turnos cuestan lo que cuesta la llave; el simulador, el juez y el arreglo son de la plataforma y se cobran siempre con la llave de la plataforma.
- **Planes:** todos, Free incluido. El tope real lo da el saldo.

### 5. Ejecución

- **Tarea Celery.** `core.pruebas.correr` (patrón de `core/tasks/gate_tasks.py`) en el worker existente, sin worker nuevo y con reintento si el candado del negocio está ocupado. Una cola propia solo si HU-AOS-103 mide que las corridas frenan los turnos reales.
- **Aislamiento entre negocios.** `tenant_scope` en toda la tarea. Los prompts del simulador, el juez y el arreglo reciben solo la configuración del agente y la transcripción de la propia corrida, nunca datos de otros negocios ni de clientes reales.
- **Bandera.** `PRUEBAS_AGENTE_ENABLED` por entorno, apagada en producción hasta pasar las pruebas del dueño.

### 6. Arreglo sugerido

Por cada resultado que falla, `core/pruebas/arreglo.py` propone un cambio tipado: `regla` (texto a agregar o cambiar en las reglas del agente), `respuesta` (una respuesta de `respuestas.md` o similar) o `dato` (precio, horario, catálogo: no se aplica solo, enlaza a la sección donde se edita). El arreglo se genera a partir de la transcripción, que puede traer texto de clientes y del catálogo: es **dato, nunca una orden**, y no se aplica solo. «Aplicar» exige el permiso de editar el agente y comprobar que el `result_id` es del negocio, **valida el cambio contra el esquema del borrador** (`AgentSpecV3`), muestra el diff al dueño y solo escribe en el **borrador** del agente (`agent_drafts`) con su confirmación, sin publicar; publicar sigue pasando por el gate y el canary (ADR-0117). «Volver a probar las fallidas» crea una corrida con solo esos escenarios contra el borrador. El arreglo se genera con `generar_estructurado` y se cobra como parte de `prueba_agente`.

### 7. API

Bajo `/api/v1/admin/agents/{agent_id}/pruebas`, con el permiso de editar el agente: `GET /`, `POST /proponer`, `POST /`, `PATCH|DELETE /{case_id}`, `POST /estimado`, `POST /corridas`, `GET /corridas/{run_id}`, `POST /corridas/{run_id}/cancelar`, `POST /resultados/{id}/aplicar`, `POST /resultados/{id}/juez-equivocado`. Errores: 409 `plan_limit_creditos`, `estimado_cambio`, `corrida_en_curso`; 422 `demasiados_escenarios`, 422 `k_fuera_de_rango`. El modelo del juez y del simulador nunca sale en la respuesta.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Correr los escenarios en `agentcore_eval` como el gate | El corredor es de una sola corrida para toda la plataforma, copia el negocio en cada corrida y sus tools son reales con pasarela simulada; el sandbox ya aísla los efectos sobre el propio negocio |
| Aserciones deterministas sobre tablas | La IA y el dueño no pueden escribirlas y el sandbox no escribe en ellas |
| Cobrar las filas del ledger de la conversación tal cual | La purga las borra y se perdería el cobro |
| Contador de cuota nuevo en una tabla | Otra regla que explicar; la cuota compartida con el playground ya existe y es una sola |
| Pruebas gratis pagadas por la plataforma (`kind='eval'`) | Costo LLM abierto y sin tope por negocio |
| Encender el gate de release con los escenarios del dueño | El gate protege la plataforma con pass^k; mezclarlos haría que un escenario mal escrito bloquee publicar |
| k = 4 siempre | Cuatro veces el costo para el dueño sin que lo pida |

## Consecuencias

**Positivas:**
- El dueño ve «pasó 18 de 20 casos de tu negocio» antes de publicar y puede arreglar con un clic.
- El cobro usa el mismo tarifario y la misma cuota que ya existen; el margen no cambia.
- Reutiliza el sandbox, el simulador y `generar_estructurado`.

**Negativas / lo que aceptamos pagar:**
- Un juez LLM puede equivocarse; se mitiga con la razón visible, el chequeo determinista primero y «el juez se equivocó».
- El estado ficticio vive en Redis: no se puede verificar con consultas a la base.
- Los resultados varían de una corrida a otra; con k = 1 hay riesgo de falso verde.
- La fila consolidada de los turnos pierde el detalle por turno (queda en la transcripción de `agent_test_results`) y el desglose por agente de las pruebas cae en el renglón sin agente.
- Hay que tocar la purga de huérfanas y el cierre masivo del playground para que respeten `prueba_corrida`.

**Qué haría que revisáramos esto:**
- Que más del 10 % de las fallas se marquen `juez_equivocado`.
- Que el error del estimado frente a los créditos reales pase del 25 % en el p90.
- Que más del 30 % de los negocios agoten la cuota de pruebas en la primera semana.
- Que se quiera correr pruebas programadas o al publicar: ahí el gate y estas pruebas se vuelven a evaluar juntos.

## Enmiendas que trae

- **ADR-0144 y ADR-0208:** la lista de tipos de uso suma `prueba_agente`; `medir_creditos` lo trata como parte del cubo del playground (fórmula del §4) y `_de_playground` excluye las conversaciones con `prueba_corrida`.
- **HU-ARCH-14:** `close_test_conversation` (las tres ramas), `reset_test_conversation` y `playground_cleanup` excluyen las conversaciones con `prueba_corrida`; `playground_cleanup` las purga con su propio umbral.
- **Simulador:** `evals/simulador.py` y su prompt pasan a `core/pruebas/`; el gate lo reutiliza desde ahí.
- **Corrección a SPEC-53:** el diseño decía que las filas de la corrida «entran en el cubo del playground» marcadas canal `test`; con la purga eso perdería el cobro, así que los turnos se consolidan en una fila `prueba_agente` antes de purgar y la cuota es el cubo derivado del §4.
