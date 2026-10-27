## Opciones directas: núcleo del modelo (2026-10-27)

- SPEC-40 / HU-OPC-01 / ADR-0172, PR 3 de la secuencia. **Nada llega a clientes**: la bandera por negocio
  (`tenants.settings.opciones_directas.habilitada`) nace apagada y aún no hay consumidor ni API.
- Migración `0112`: `option_templates`, `option_template_versions` (inmutables, `prevent_mutation`),
  `option_sends` y `option_activations`, con RLS forzada, FKs compuestas (también hacia `message_templates`, que gana `UNIQUE(tenant_id, id)`) y sin
  `DELETE` para `app_runtime` (`option_sends` tampoco recibe `UPDATE`).
- `core/kernel/opciones/`: definición pydantic, lint (`{path, code, severity, mensaje}`), mapeo puro a
  interactivo (`button` ≤ 3, `list` 4–10) y a componentes de Meta, payload `opt:`/`optc:`/`optn:` firmado.
- `MCPTool.apta_para_opcion` y `sensible` (por defecto `False`) y `option_safe` en las tools declarativas;
  **ninguna tool existente queda apta**. Una declarativa con método distinto de GET nace `sensible`.
- El linter de procedimientos reserva `opt:`, `optc:` y `optn:` (disparador y `setBy: interactive:`).
