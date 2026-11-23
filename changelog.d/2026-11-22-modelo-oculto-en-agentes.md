## El negocio ya no ve el modelo de sus agentes (2026-11-22, HU-BILL-24)

- `GET /admin/agents/{id}`, `GET/PUT /admin/agents/{id}/draft` y `GET /admin/agents/{id}/versions/{vid}` le devuelven el contenido sin `model` (AgentSpec v3), ni `modelPolicy` ni `routing.coprocessor.model` (KernelSpec) a quien no actúa como master (ADR-0210 §1). El esquema de la respuesta no cambia.
- Al guardar su borrador, el servidor conserva el modelo guardado e ignora el que mande quien no es master. El master lo sigue viendo y editando.
