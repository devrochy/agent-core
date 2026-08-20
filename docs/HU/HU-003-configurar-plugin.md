# HU-003 · Configurar un plugin desde el panel

**Como** admin, **quiero** un formulario generado automáticamente para cada plugin,
**para** configurarlo sin tocar archivos ni pedirle nada a un developer.

## Criterios de aceptación

- [ ] /admin/plugins lista todos los plugins con estado
      (detected/configured/active/disabled/error) y el error exacto si lo hay.
- [ ] "Configurar" muestra un formulario Bootstrap generado desde el `config_schema`
      Pydantic: SecretStr→password, bool→checkbox, Literal→select, int→number,
      description→texto de ayuda.
- [ ] Guardar valida contra el schema; errores de validación NO persisten nada.
- [ ] La config se guarda cifrada (Fernet) en `plugin_configs.config_encrypted`.
- [ ] Un campo password vacío al re-guardar CONSERVA el secreto anterior.
- [ ] "Guardar y activar" ejecuta migraciones del plugin + `on_activate()`; el menú
      lateral muestra las entradas del plugin inmediatamente.
- [ ] "Desactivar" retira menú, tools, guardrails y suscripciones al instante.

## Flujo técnico

`core/admin/form_generator.py` (render + parse) → `plugin_manager.save_config()`
→ `plugin_manager.activate()` → `mcp_host` + `admin_menu`.
