## Vista de recepción del Coordinador (2026-11-21, HU-AOS-89)

- Cuando la conexión no tiene especialistas, el Coordinador atiende con su vista de recepción: un `agentspec/3` en memoria, con los ids reales del Coordinador, el set cerrado de 7 tools (`crear_lead`, `get_customer_profile`, `update_customer_profile`, `pedir_telefono`, `responder_aviso_datos`, `escalate_to_human` y `set_conversation_note`), sin packs ni procedimientos, y el playbook del kernel `recepcion-coordinador` (sin override del negocio). Reemplaza el corte en silencio de HU-AOS-88.
- `update_customer_profile` desde la recepción solo guarda nombre, teléfono y correo: lo recorta el host.
- El `usage_ledger` anota esos turnos como `coordinador@recepcion:<versión del Coordinador>`.
- La API responde 409 `coordinador_inmutable` a instalarle habilidades al Coordinador o cambiarle el flujo o el horario.
