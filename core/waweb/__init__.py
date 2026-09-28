"""Canal WhatsApp Web no oficial (`waweb`, ADR-0074).

Lógica del connector: política anti-baneo, sesiones y handoff de salida. El
adaptador de canal vive en `packages/channel-waweb` (entry_point
`agent_core.channels`); acá está el soporte del core (sesiones, config).
"""
