## Retiro de las pantallas del AgentSpec v2 (2026-11-22, HU-AOS-91, parte D1)

El turno no lee el AgentSpec v2 desde HU-AOS-90. Por decisión del dueño, lo que se editaba en él se retira ya.

- **Rutas de la API que responden 410 `v2_retirado`:**
  - `GET|PUT /admin/settings/advanced/spec` (el editor crudo).
  - `POST /admin/settings/advanced/versions/{id}/activate`.
  - `PUT /admin/settings/advanced/guardrails`.
  - `PUT /admin/settings/skills` (las tools del negocio).
  - `GET|PUT /admin/settings/declarative-tools`.
- **`GET /admin/settings/advanced`** sigue devolviendo el LLM propio, el flujo (armado con las habilidades del negocio) y las corridas de evals. `versions`, `guardrails_text` y `spec` vienen vacíos, y `GET /admin/settings` trae `skills: []`, para no romper la APK instalada.
- **Panel SSR:** `POST /admin/spec`, `POST /admin/configuracion/habilidades` y `POST /admin/configuracion/spec/activar` responden 410.
- **CLI:** `agent-core agent activate|rollback` queda retirado. Las versiones ahora son de cada agente y las releases se revierten desde Conexiones.
- **Pendiente:** los guardrails y las declarativas por agente (`AgentSpecV3`) van en una HU nueva. El catálogo de acciones de Plantillas de opciones sin `agent_id` sigue leyendo los bindings del spec, que ahora no tienen dónde editarse. Pasa al agente en la parte D3.
