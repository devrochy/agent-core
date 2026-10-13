### Agregado

- Centro de pruebas: «Preparar mis datos» crea, solo en test, un negocio de prueba por tester con Comercio,
  Agenda y Cobranza, datos de ejemplo y cuentas de dueño, gestor y colaborador, con contraseñas que se muestran
  una sola vez (`/api/v1/admin/qa/datos`, HU-QA-07, ADR-0135).
- `seed_data` de Agenda (tipos de cita, citas y bloqueos) y de Cobranza (cartera, caja, gestor y clientes con
  préstamos al día y en mora), y fragmentos `presets/qa.json` de los tres packs.
- Catálogo de pruebas: campos `entorno`, `area` y `requiere`, y la sección `datos` con la guía de datos
  externos (HU-QA-08). Migración 0089.
