## Superpoderes por enfoque en el editor de agentes (2026-10-23)

- HU-AOS-50 entrega 1, ADR-0155 (enmienda ADR-0139). `GET /api/v1/admin/agent-superpowers?agent_id=` devuelve solo
  los superpoderes de la plantilla del agente (Ventas, Agendamiento, Cobranza, Suscripción); sin `agent_id` no cambia.
  Publicar un agente de plantilla con un superpoder de otro enfoque responde 422 `superpoder_fuera_de_enfoque` junto
  al campo. Las keys que no son de una plantilla (`general`, `coordinador`, especialistas por API) no se restringen.
