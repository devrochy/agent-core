## Importar datos existentes por agente desde un CSV (2026-10-28)

- En test (`QA_TESTERS_ENABLED`), `GET …/admin/agents/{id}/importar-datos` (+ `/plantilla.csv`), `POST …/validar`,
  `POST …/importar-datos` y `DELETE …/importar-datos` importan productos, tipos de cita, clientes y préstamos
  (sin teléfono) y planes desde un CSV, con vista previa que muestra los errores fila por fila (HU-QA-12, ADR-0180).
- Idempotente por clave natural: lo que ya existe se omite. Lo importado es un lote propio
  (`test_data_records.origen = 'importacion'`) que «Generar datos de prueba» no toca.
- Migración 0119: el CHECK de `test_data_records.origen` admite `importacion`.
- Retirar datos de prueba libera el SKU de las variantes de Comercio (`<sku>~retirado-<id8>`), para poder volver a importar.
- Una cartera de cobranza retirada ya no deja clientes vivos huérfanos.
