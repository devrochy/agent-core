## Seguridad: la foto de un producto ya no llega a archivos de otro negocio (2026-10-25)

- Hallazgo del auditor multi-tenant (severidad alta, solo en develop): `image_key` aceptaba
  `<mi-slug>/productos/../../<otro-slug>/…` porque solo se comprobaba el prefijo de texto. El producto
  guardaba la key ajena, el panel recibía una URL firmada del archivo del otro negocio y, al reemplazar
  o quitar la foto, `borrar` podía eliminarlo. Bastaba un admin con escritura en el catálogo.
- `core/almacenamiento`: `abrir`/`borrar` rechazan keys con segmentos vacíos, `.` o `..`, barras
  invertidas, NUL o ruta absoluta (antes un NUL daba un 500). Nuevo `dentro_de=` opcional (con
  `carpeta_de(kind, slug)`) que exige que la ruta ya resuelta quede dentro de la carpeta del negocio,
  también frente a enlaces simbólicos. `borrar` solo borra archivos, nunca carpetas.
- `pack_commerce`: `_key_producto_valida` valida la forma (`<slug>/productos/<archivo>`) y abre con
  `dentro_de`; el borrado de la foto anterior también. `url_foto` no firma una `image_key` mal formada
  (cae al link externo), así que las filas viejas con una key sospechosa no se sirven ni se borran.
- Sin migración de datos. Para detectar filas ya contaminadas:
  `SELECT id, tenant_id, image_key FROM pack_commerce_products WHERE image_key IS NOT NULL AND image_key !~ '^[a-z0-9][a-z0-9-]*/productos/[A-Za-z0-9_-]+\.[A-Za-z0-9]+$';`
  (con un rol que salte la RLS; las fotos legítimas son `<slug>/productos/<uuid>.<ext>`).
  Y las que tienen una key bien formada pero de otro negocio (solo posible por escritura directa en la base;
  `url_foto` valida solo la forma, el slug se exige al guardar y en `borrar`):
  `SELECT p.id, p.tenant_id, p.image_key FROM pack_commerce_products p JOIN tenants t ON t.id = p.tenant_id WHERE p.image_key IS NOT NULL AND split_part(p.image_key, '/', 1) <> t.slug;`
- Tests: `tests/test_storage_key_traversal.py` y `tests/integration/test_pack_commerce_foto_traversal.py`.
