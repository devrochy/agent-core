"""Centro de pruebas de la plataforma (SPEC-30, ADR-0133).

- `catalogo`: lee y valida `11-pruebas/catalogo.yaml` de solvershub-docs (esquema v1 y v2).
- `estados`: reglas puras de estado (paso → prueba → ciclo) y progreso.
- `service`: ciclos, ejecuciones por tester, pasos, capturas, informe e historial.
- `evidencias`: capturas en `TENANTS_ROOT/.plataforma/pruebas/<run_id>/`.
- `informe`: el informe congelado de una ejecución, en JSON y Markdown.
"""
