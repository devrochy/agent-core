## Importar el catálogo desde un CSV con mapeo de IA, también en producción (SPEC-45, ADR-0194 §2, 2026-11-14)

- **Mapeo de columnas** (`core/qa/mapeo_columnas.py`, playbook del kernel `mapear_columnas`): el dueño sube
  su hoja con sus cabeceras. Primero se reconocen por nombre y por los sinónimos que declara el pack
  (`PlantillaDeImportacion.sinonimos`); solo si quedan cabeceras sin reconocer y columnas libres, una
  llamada a la IA con las cabeceras y 5 filas de muestra. La IA solo puede asignar columnas libres de la
  plantilla, cada una una vez; si falla, esas cabeceras quedan sin asignar. Se mide en `usage_ledger`
  (`kind='generacion'`, `result.proposito='mapeo_columnas'`) con la llave del negocio si la tiene.
- **Precarga sin estado en el servidor:** `POST …/importar-datos/mapear` devuelve el mapeo, las filas
  normalizadas y su vista previa; el cliente las edita y las confirma en JSON con
  `POST …/importar-datos/filas/validar` y `POST …/importar-datos/filas`, que vuelven a validar contra el
  esquema del pack (mismos errores por fila, 422 `archivo_con_errores` salvo `solo_validas`,
  idempotente por clave). Auditoría con `fuente = 'precarga'`.
- **Producción:** las rutas de importación responden sin `QA_TESTERS_ENABLED` solo si la plantilla del
  agente se declara `en_produccion` (hoy, `productos` de commerce); las demás siguen en 404.
  `PlantillaImportacionOut.en_produccion` lo expone para que el cliente pinte lo que el servidor declara.
- **API (compatible):** 3 rutas nuevas y un campo nuevo en `PlantillaImportacionOut`.
- **Despliegue:** sin migración. Depende del helper de IA estructurada (PR #764).
