## Centro de pruebas: modelo, API e importación del catálogo (2026-10-13)

- **SPEC-30, HU-QA-01 y HU-QA-02.** Base del centro de pruebas del panel. Este PR no trae el rol tester (HU-QA-04)
  ni la pantalla (HU-QA-03).
- **Migración 0087 con nueve tablas de sistema `qa_*`, sin RLS** (ADR-0133). Guardan catálogo por versión,
  ciclos, ejecuciones por tester con pasos, notas y capturas, informe congelado, configuración de test y de
  producción, e historial de solo agregar.
- **API `/api/v1/admin/qa/*` con la sección global `qa`:**
  - catálogo con el estado consolidado del ciclo;
  - mi ejecución: empezar, marcar pasos, nota, capturas y enviar el informe;
  - informes en JSON y Markdown;
  - historial;
  - ciclos: listar, y cerrar abriendo uno nuevo;
  - configuración de test y prod;
  - importación, solo para master.
- **CLI `agent-core qa validar|importar <catalogo.yaml>`.** La importación es idempotente por contenido.
- **Capturas en `TENANTS_ROOT/.plataforma/pruebas/<run_id>/`:** jpeg, png, webp o pdf, hasta 5 MB. Se sirven
  solo con sesión.
- **Correo de sistema:** al enviar un informe se avisa a los masters, con el enlace `PANEL_BASE_URL/pruebas/informes/<id>`.
- **`pyyaml` pasa a dependencia directa.** Antes llegaba de forma transitiva.
