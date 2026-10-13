## Datos de prueba por tester y catálogo por entorno y sección (2026-10-13)

- **SPEC-30, HU-QA-07, ADR-0135.** «Preparar mis datos» crea, solo en test, un negocio de prueba por tester con
  Comercio, Agenda y Cobranza, datos de ejemplo y cuentas de dueño, gestor y colaborador en el dominio reservado
  `pruebas.solvershub.test`. Las contraseñas se muestran una sola vez; «Contraseñas nuevas» las rota y cierra
  sesiones (`/api/v1/admin/qa/datos`).
- **Las cuentas de prueba siguen al tester:** sin `QA_TESTERS_ENABLED` no entran, y revocar al tester las desactiva.
- **`seed_data` de Agenda y de Cobranza** (tipos de cita, citas y bloqueos; cartera, caja, gestor y clientes al día,
  en mora y sin préstamo) y fragmentos `presets/qa.json` de los tres packs.
- **HU-QA-08.** El catálogo suma `entorno`, `area`, `requiere` y la sección `datos` (guía de datos externos).
  Migración 0089.
