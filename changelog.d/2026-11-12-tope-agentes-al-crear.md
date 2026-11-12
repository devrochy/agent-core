## El tope de agentes también bloquea la creación del borrador (2026-11-12)

- **Planes:** al llegar al tope de agentes publicados del plan, `POST /admin/agent-templates/{key}/agents`
  y `POST /admin/agents/from-script` responden `409 plan_limit_agentes` sin crear el agente ni habilitar
  habilidades. Antes solo se bloqueaba al publicar.
- **API:** `GET /admin/agents/overview` trae `cuota_agentes` (`used`, `limit`, `allowed`) para que el panel
  y la APK avisen antes de abrir el asistente, sin exigir el permiso de Plan.
- Sin migración: el tope sigue contando agentes publicados con instancia; el General y el Coordinador no cuentan.
