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

**Positivas:** una opción nueva nace explicada en los dos clientes; los rangos tienen una fuente (HU-CFG-03 hace que el runtime y la API la lean); una configuración conversacional futura sabe qué puede tocar.

**Negativas / lo que aceptamos pagar:** texto de interfaz en código Python del core (por eso el límite de importación y la prueba); hay que registrar cada clave nueva (la prueba de consistencia lo exige).

**Qué haría que revisáramos esto:** que las etiquetas necesiten traducción por negocio, o que haya opciones por negocio que cambien con tanta frecuencia que justifiquen una tabla.

## Impacto en el código

- `core/config_registro/` (modelo, 31 opciones, catálogo).
- `core/api/routers/settings.py` (`GET /admin/settings/catalog`) y `docs/openapi.json`.
- Pruebas: `tests/test_cfg02_registro_opciones.py`, `tests/integration/test_cfg02_catalogo_api.py`.
