# ADR-0180 — Importar datos existentes por agente desde un CSV: plantillas por pack, vista previa por fila y lote propio

- **Estado:** propuesto (HU-QA-12, SPEC-32). El dueño decidió el alcance el 2026-10-28: los cuatro módulos,
  solo CSV, omitir las claves que ya existen y un botón aparte para borrar lo importado.
- **Fecha:** 2026-10-28
- **Contexto:** SPEC-32 «Datos de prueba por agente» (`spec.md` y `design.md`) en `solvershub-docs`.
  **Usa:** ADR-0146 (formas de datos, `test_data_records`, lock por agente, `QA_TESTERS_ENABLED`),
  ADR-0124 (hooks opcionales de pack con contrato en el core), ADR-0135 (solo en test), ADR-0083/0110
  (`seed_data` de los packs), invariantes 1, 5, 7 y 8.
  **No enmienda** ADR-0146: añade un origen de lote y un contrato paralelo.

## Contexto

«Generar datos de prueba» (ADR-0146) arma un catálogo, una agenda o una cartera inventados según la descripción
del negocio. Un negocio que ya tiene su catálogo o su cartera en una hoja de cálculo quiere ver a su agente
trabajar con **esos** datos. Hoy solo existe el CSV de productos del panel de Comercio (`name`, `price`), que
omite en silencio las filas malas, no deja rastro de lo que creó y no sirve para citas, cartera ni planes.

## Decisión

1. **Contrato pack ↔ core** (`core/qa/contrato_importacion.py`, como `contrato_datos.py`; no toca
   `agentcore_contracts`). Dos hooks opcionales nuevos en `BaseModule`:
   - `plantillas_de_importacion()` → `PlantillaDeImportacion`: columnas, filas de ejemplo, esquema pydantic
     por fila (mensajes de error en español), clave natural y la `modo` de la `FormaDeDatos` a la que
     pertenece. El agente importa con la plantilla de su forma: ventas → `productos`, agenda →
     `tipos_de_cita`, cobranza → `clientes_y_prestamos`, suscripciones → `planes`.
   - `importar_filas(tenant, plantilla, filas, contexto, simular)` → `ResultadoDeImportacion` (filas nuevas,
     existentes, errores del pack y registros creados). Con `simular` no escribe: es la vista previa.
   Retirar reusa `retirar_datos_de_prueba`.
2. **El core se ocupa del archivo** (`core/qa/importar_datos.py`): tope de 1 MB y 500 filas; UTF-8 (con o sin
   BOM) o Windows-1252; separador `,` `;` o tab; cabeceras normalizadas (sin tildes, minúsculas); las
   desconocidas se ignoran y se avisan; validación fila por fila con reporte `{fila, columna, mensaje}`;
   clave repetida dentro del archivo = error de la segunda; números y fechas como en Excel es-CO
   (`1.500.000`, `45000,5`, `28/10/2026`). El archivo es dato no confiable (invariante 7).
3. **Sin estado en el servidor.** El panel sube el archivo a `…/importar-datos/validar` (vista previa, no
   escribe) y, al confirmar, otra vez a `…/importar-datos`, que vuelve a validar. Con errores responde 422
   `archivo_con_errores` salvo `solo_validas=true`.
4. **Idempotente por clave natural**: SKU en productos y planes, nombre en tipos de cita, cartera +
   documento en clientes. Una clave que ya existe (hecha a mano, generada o importada antes) se reporta como
   «ya existía» y **no se modifica**. Subir dos veces el mismo archivo no duplica. Un SKU que pertenece a un
   producto eliminado a mano es error de la fila: no se resucita lo que el negocio borró.
5. **Lote propio.** Cada importación escribe en `test_data_records` con `origen = 'importacion'` (migración
   0119: amplía el CHECK; sin tablas ni RLS nuevas). «Generar datos de prueba» y su borrado excluyen ese
   origen: lo importado es del dueño. `DELETE …/importar-datos` retira solo lo importado.
6. **Retirar libera el SKU.** El SKU es único por negocio aunque la variante esté inactiva, así que
   `retirar` de Comercio renombra el SKU de la variante retirada (`<sku>~retirado-<id8>`). Así «borrar y
   volver a importar el mismo archivo» crea los productos otra vez. También se aplica a lo generado, que
   usa SKU `PRUEBA-…` y nunca chocaba.
7. **Cobranza sin teléfono ni correo.** La plantilla no los tiene y, si el archivo los trae, se ignoran: ningún
   recordatorio puede llegar a un número real. Una cartera nueva arranca con la caja justa para que los
   préstamos importados no la dejen negativa; una cartera existente se reutiliza y no se retira si después
   le crearon clientes a mano.
8. **Solo test y permisos.** Sin `QA_TESTERS_ENABLED` todas las rutas responden 404. Ver el estado: sección
   `agents`. Importar o borrar: `agents` y la sección del pack del módulo, ambas en escritura (el mismo
   criterio de `_permiso_modulo`). El agente se busca con `tenant_id` explícito además de la RLS; el de otro
   negocio es 404 sin eco y no escribe nada.
9. **Auditoría.** Una fila de `audit_log` (`qa_importar_datos`) por importación y por borrado, con el
   actor, el agente, la plantilla, el lote y los conteos, sin el contenido del archivo; más un log
   estructurado equivalente.

## Alternativas descartadas

- **Excel (`.xlsx`).** Pedido en la HU, pero no hay `openpyxl` en las dependencias y Excel exporta CSV; el
  dueño eligió solo CSV (2026-10-28).
- **Extender `POST /products/import`.** Omite filas en silencio, no registra lote y vive en un solo pack.
- **Actualizar las filas que ya existen.** Más poder, más riesgo de pisar lo del negocio; el dueño eligió omitir.
- **Estado de la vista previa en el servidor (borrador).** Una tabla o caché más para evitar subir el archivo
  dos veces; con 1 MB de tope no compensa.
- **Hash del archivo para detectar reimportaciones.** La clave natural por fila cubre también archivos
  parecidos o ampliados.

## Consecuencias

- Una migración (0119) que se aplica al arrancar la API; recrear el CHECK de una tabla pequeña es instantáneo.
- Importar datos reales en test los mezcla con datos de prueba: si esto sale a producción hay que reabrir
  D-SPEC-32-2 (los datos serían reales y no se marcarían como de prueba).
- Cada pack nuevo con una forma de datos puede declarar su plantilla sin tocar el core.
- Tests: unitarios de lectura y validación del CSV; de integración con los cuatro módulos, errores,
  idempotencia, borrado, permisos y aislamiento entre negocios.
