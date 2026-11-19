# ADR-0166 — Registro de opciones de configuración del negocio

- **Estado:** propuesto (se acepta al mergear HU-CFG-02 y firmarse D-47 de solvershub-docs).
- **Fecha:** 2026-10-25
- **Contexto:** SPEC-37, HU-CFG-02, D-SPEC-37-1, D-SPEC-37-2 y D-SPEC-37-3. **Usa:** ADR-0049 (Configuración por pestañas), ADR-0050 (persona del spec) y ADR-0092 (acción al agotar guardrails).

## Contexto

La configuración del asistente está repartida en tres lugares: columnas de `AppSettings`, dos JSONB libres (`behavior_config`, `escalation_config`) y la persona del `AgentSpec`. No hay una lista de las opciones: cada pantalla (panel y APK) describe las suyas a mano, los rangos de los umbrales están escritos cuatro veces y seis claves con efecto real (`palabras_de_baja`, `waweb_*`) no tienen pantalla. El inventario de la SPEC-37 lo documenta.

## Decisión

1. **Un registro declarativo en el core**, `core/config_registro/`: un modelo `Opcion` (`extra="forbid"`) y las 31 opciones de Identidad y comportamiento. Cada una dice su clave, grupo, tipo, etiqueta, explicación, valores con etiqueta, rango, valor por defecto, dependencia, quién la edita y dónde se guarda.
2. **El registro es una vista, no un almacén.** No hay tablas ni migraciones. Los valores siguen en las columnas, en `behavior_config`/`escalation_config` y en el spec; los escritores actuales no cambian.
3. **Los valores salen del runtime.** Las listas y rangos se derivan de las constantes que ya usa el código (`DEBOUNCE_DELAY_MS`, `CAPS`, `TOGGLES`, los umbrales de dificultad, `core/waweb/policy.py`, los literales del contrato `Persona`). Los guardrails apagables se calculan por negocio con los packs habilitados.
4. **Etiquetas y explicaciones viven en el core**, se sirven por `GET /api/v1/admin/settings/catalog` y las pintan el panel y la APK. Son texto para personas: **nunca entran a un prompt** (invariante 3). Una prueba verifica que el runtime de turnos (`llm`, `orchestrator`, `kernel`, `agents`, `gateway`, `mcp`, `memory`, `waweb`, `tasks`) no importa el paquete.
5. **El catálogo se filtra por rol.** Las opciones `editable_por="master"` (`waweb_*`) solo las recibe el master. La sección exigida es `settings` (403 sin ella).
6. **Prueba de consistencia con `ast`.** Recorre los módulos que leen `behavior_config` y `escalation_config` y compara las claves literales con el registro: una clave leída y no registrada, o registrada y sin lector, hace fallar la prueba y nombra la clave y el archivo. No ve claves armadas en ejecución.
7. **`version` del catálogo** es una huella del contenido, para que la APK sepa si su copia guardada sigue vigente.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla en la base de datos | Obliga a una migración con RLS para metadatos que no cambian por negocio, y agranda la brecha hacia producción. |
| JSON como los presets | Los valores derivados (`DEBOUNCE_DELAY_MS`, `TOGGLES`) habría que copiarlos a mano y se desfasarían. |
| Etiquetas en `lib/copy.ts` del panel y de la APK | Se redactan y mantienen dos veces; la APK instalada no se entera de un cambio. |

## Consecuencias

**Positivas:** una opción nueva nace explicada en los dos clientes; los rangos tienen una fuente (HU-CFG-03 hace que el runtime y la API la lean); una configuración conversacional futura podría apoyarse en él, pero eso exige un ADR propio y expondría solo clave, tipo y rango al modelo, nunca etiquetas ni explicaciones (decisión 4).

**Negativas / lo que aceptamos pagar:** texto de interfaz en código Python del core (por eso el límite de importación y la prueba); hay que registrar cada clave nueva (la prueba de consistencia lo exige); las listas de moneda y zona horaria son cerradas y agregar un mercado exige un despliegue (los clientes deben mostrar un valor actual fuera de la lista); las validaciones apagables salen con su id técnico como etiqueta hasta que el registro de guardrails traiga una descripción.

**Pendiente para HU-CFG-03:** que el runtime lea rangos y defaults del registro sin importar los textos. Para no invertir la dependencia ni romper la prueba de importación, los límites se separan en un módulo hoja (sin textos ni imports del runtime) que el runtime y el registro comparten.

**Qué haría que revisáramos esto:** que las etiquetas necesiten traducción por negocio, o que haya opciones por negocio que cambien con tanta frecuencia que justifiquen una tabla.

## Impacto en el código

- `core/config_registro/` (modelo, 31 opciones, catálogo).
- `core/api/routers/settings.py` (`GET /admin/settings/catalog`) y `docs/openapi.json`.
- Pruebas: `tests/test_cfg02_registro_opciones.py`, `tests/integration/test_cfg02_catalogo_api.py`.

## Adenda 2026-10-25 · HU-CFG-07: el Coordinador (KernelSpec) y las banderas del Agent OS

Esta adenda reemplaza «31 opciones de Identidad y comportamiento» de la decisión 1: el registro suma el Coordinador y las banderas. El resto del ADR sigue igual.

1. **Se describe, no se escribe.** El `KernelSpec` (`kernelspec/1`, congelado) vive en la versión del agente `core` de cada **conexión** y solo cambia con una versión nueva y su release (gate pass^k de HU-AOS-21, aún abierto). Las banderas (`tenants.aos_runtime`, `aos_procedures`, `aos_routing`) las escribe una sola función, `aos_fijar_banderas`, que valida el orden y deja la fila de auditoría. El registro no añade ningún escritor: `almacen` solo apunta (`kernelspec:<ruta.punteada>` y `tenants:<columna>`).
2. **Dos grupos nuevos:** `coordinador` (19 escalares del KernelSpec) y `agent_os` (3 banderas, solo master). Los clientes de hoy iteran por sus grupos conocidos, así que no los afectan; cambian la huella `version` del catálogo.
3. **`ambito` se deriva del almacén** (`conexion` si es `kernelspec:`, `negocio` si no) y no se guarda aparte, para que no haya dos fuentes que se contradigan. La API lo expone como campo de solo lectura.
4. **Rangos y defectos salen del contrato**, leyendo `Ge`/`Le` y `default` de `KernelSpec.model_fields`; los valores de las banderas salen de `RUNTIMES` (`core/admin/agent_os.py`) y `MODOS` (`core/kernel/router.py`). La cota que el contrato pone no es siempre la efectiva: cada enlace entre agentes puede bajar los límites de `a2a.*` (ADR-0116), y la regla `strongScore ≥ minScore` solo la valida el KernelSpec completo.
5. **Lo que no se ofrece, con motivo y vigilado.** `EXCLUIDAS_DEL_KERNELSPEC` lista cada hoja del contrato fuera del registro: la voz (`voice.*`, duplicaría `persona_*`), los ids de modelo en texto libre (ADR-0086, ADR-0132), los playbooks (selección de prompt, invariante 3), `routing.defaultAgent` (necesita lista calculada por conexión), `routing.rules` (DSL), y los dos presupuestos que admiten «sin tope». Una prueba exige que toda hoja del KernelSpec esté registrada o excluida: un campo nuevo del contrato no pasa sin decisión.
6. **Quién ve qué.** Los topes de costo (`routing.coprocessor.maxCostUsd`, `a2a.turnBudgetUsd`, `a2a.turnBudgetTokens`) y las banderas son `editable_por="master"`. El dueño ya fija un presupuesto por turno en el AgentSpec v2 (`policy_budget`); si debe poder fijar también estos, es una decisión de producto aparte.
7. **Valores por conexión.** Servir los *valores* del KernelSpec exigirá leerlos bajo `tenant_scope` y con la conexión explícita, y distinguir los negocios `legacy` (donde el KernelSpec se deriva en memoria de la persona). Queda para la HU del formulario.
8. **`AOS_CONNECTION_ROUTING`** (variable de despliegue) no es una opción: se explica en el texto de `aos_runtime`.
