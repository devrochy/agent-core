## Probar el borrador de un agente sin publicarlo (2026-11-14)

- HU-AOS-72: con `agent_id`, `GET /admin/playground`, `POST /admin/playground/message`, `/reset` y
  `/close` prueban el **borrador** de ese agente sin publicarlo: el turno corre con una release efímera
  armada en memoria (sin release, versión ni instancia), directo a ese agente (sin el Coordinador),
  siempre simulado (sandbox de HU-ARCH-14) y sin cupo de agentes.
- Exige escritura en `settings` (la sección del editor de agentes). Un agente que no es un especialista
  activo del negocio con borrador es 404 `agente_no_encontrado`, sin eco del id.
- Adenda HU-AOS-72 de ADR-0191 y nota en ADR-0117.
