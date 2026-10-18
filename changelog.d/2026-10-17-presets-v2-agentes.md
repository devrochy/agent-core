## Presets v2: agentes especialistas y reglas del Coordinador (2026-10-18)

- HU-AOS-46 (ADR-0139): los presets declaran sus agentes «para un fin» (`agentes`): comercio y restaurante →
  Ventas; cobranza → Cobranza; suscripciones → Ventas, Agenda y Suscripciones. Los cuatro presets pasan a `version` 2.
- Plantillas de agente como configuración del core (`core/presets/plantillas/`), sin prompts.
- En un negocio con `aos_runtime = 'v3'` y `AOS_CONNECTION_ROUTING`, aplicar el preset crea cada especialista
  (versión derivada del General, con sus procedimientos y acciones), su instancia en las conexiones con sus
  habilidades (el General sigue por defecto), las reglas `keywordAny` del Coordinador y la release candidata de
  cada conexión, sin activarla. Idempotente: no duplica ni pisa lo que el dueño cambió.
- CLI `agent-core db presets-v2 --tenant <slug>|--todos [--preset <id>] [--aplicar]` (dry-run por defecto) para
  negocios existentes.
- Con las banderas apagadas no cambia nada. Sin migración.
