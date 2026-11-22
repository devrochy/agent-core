## Datos del negocio en app_settings.agent_defaults (2026-11-22, HU-AOS-91, parte C1)

- Voz, tope de largo, frases prohibidas, modelo, memoria, políticas y playbooks base del negocio pasan de `agent_specs` a `app_settings.agent_defaults` (JSONB, migración 0152 con backfill desde el spec activo; ADR-0206 §6). Los lee `core/agents/defaults_negocio.py` y siembran el KernelSpec del Coordinador y el contenido de un agente nuevo.
- Configuración (`/settings/identity`, `/brain`, `/memory`, el asistente por preguntas), el alta, los presets, `agent-core configure tone` y el panel SSR escriben ahí; ya no crean versiones de `agent_specs` para esos campos. Un guardado parcial solo cambia lo que viene.
- El registro de opciones cambia el almacén `spec:*` por `agent_defaults:*` (visible en `GET /admin/settings/catalog`).
- **Despliegue:** la migración 0152 exige un rol con BYPASSRLS, como 0147.
