## Retiro de las rutas `agent-templates` (HU-AOS-94) (2026-11-26)

- Se quitan `GET /api/v1/admin/agent-templates` y `POST /api/v1/admin/agent-templates/{key}/agents`, `deprecated`
  desde HU-AOS-94. Usa `GET /api/v1/admin/agent-types` y `POST /api/v1/admin/agent-types/{key}/agents`, que traen lo
  mismo. Una llamada a las rutas viejas responde 404.
- Salen del contrato los esquemas `AgentTemplate` y `Page_AgentTemplate_`; el panel y la APK regeneran sus tipos.
