## Las habilidades del negocio se leen en un solo punto (2026-11-22, HU-AOS-91, parte B)

- `packs_del_negocio()` (`core/agents/instalaciones_agente.py`) es la única lectura de las habilidades del negocio fuera del turno: crons de los packs, pagos, cuota `plugins` del plan, dashboard, Clientes, datos del cliente, procedimientos, admin, editor, presets, releases y CLI (ADR-0206 §5).
- **Sin cambio de conducta:** por ahora sigue leyendo `agent_specs.packs`. La parte C mueve los escritores (alta, presets y «activar») y cambia la fuente a las instalaciones de los agentes activos (`packs_instalados`), según la decisión del dueño del 2026-11-22.
- Nuevo `agent-core db packs-sin-instalacion [--tenant|--todos] [--aplicar] [--quitar-sobrantes]`: el precheck del cambio de fuente. Reporta lo que falta y lo que sobra por negocio; con `--aplicar` instala en los especialistas activos las habilidades transversales que faltan; con `--quitar-sobrantes` desinstala lo que sobra.
