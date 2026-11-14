## Importar el catálogo desde fotos: una foto = un producto, cotizado y cobrado (SPEC-45, ADR-0194 §2 y §3, 2026-11-14)

- **Rutas** (con la misma compuerta de producción que la importación: plantillas `en_produccion`):
  - `GET …/importar-datos/fotos/cotizacion?fotos=N`: créditos de N fotos, por foto, si el negocio usa
    llave propia, usado/límite/disponible del mes y si alcanza. No escribe.
  - `POST …/importar-datos/fotos` (una foto): la guarda con el perfil `producto` (nombre = sha256
    del contenido), cotiza y verifica el crédito (409 `plan_limit_creditos` sin llamar al proveedor),
    la reconoce con visión (playbook del kernel `reconocer_foto`, esquema de la plantilla) y
    devuelve la fila propuesta para la precarga con la foto. 422 `foto_no_reconocida` si no se ve un
    producto (se cobra, el proveedor ya cobró); 503 `vision_no_respondio` sin cobrar.
- **Idempotencia:** la misma foto otra vez se responde desde el ledger (`result.salida`) sin llamar ni
  cobrar (`repetida = true`).
- **Ledger:** `kind = 'vision'` con créditos reales y `result = {proposito: 'foto_catalogo', image_key,
  agent_id, plantilla, salida}`. `reconocer_imagen` gana `guardar_salida`.
- **Contrato de importación:** `PlantillaDeImportacion` gana `esquema_foto`, `desde_foto` y `columna_foto`
  (columna interna, fuera de la plantilla descargable); `PlantillaImportacionOut.importa_fotos`.
- **Commerce:** la plantilla `productos` importa fotos (`ProductoEnFoto`: nombre, descripción, categoría
  y precio solo si se ve). La fila propone el SKU `FOTO-<huella>` y la columna `imagen`; al importar se
  valida que la foto sea de este negocio y queda como `image_key` del producto (`seed_catalog`).
- **Despliegue:** sin migración propia; necesita la 0129 de HU-BILL-16 (#765) y el PR del CSV (#767).
