## Mi agente y su data (2026-10-18)

- HU-AOS-47, ADR-0140. El dueño ve cada agente y lo que hizo: API de lectura por agente
  `GET /api/v1/admin/agents/overview` y `GET /api/v1/admin/agents/{ref}/{conversations,customers,tasks,records,usage}`,
  paginada y filtrable por conexión y período (30 días por defecto, máximo 366). `ref` es el id del agente o `general`.
- Procedencia de los registros que crea un agente (pedidos, leads, citas, promesas) en la tabla nueva
  `agent_record_origins` (migración 0095, append-only, RLS). La escribe el loop solo con el runtime v3; cada pack declara
  en la tool qué crea (`MCPTool.crea_registro`) y cómo reconocer un registro nuevo.
- El General absorbe lo no atribuido (turnos legacy o de antes de v3): un negocio sin agentes ve un General con lo que haya.
- Sección de permisos nueva `agents` («Agentes»). Conversaciones y clientes exigen además `conversations`; los registros
  se filtran por el permiso del pack. El costo en USD solo sale con la sección `costs` (sin ella va en null).
- Índice `ix_conversation_tasks_owner` (CONCURRENTLY) para las tareas por dueño.
- Pendiente: `/admin/plan` con `agentes` y `colaboradores` (depende de HU-BILL-05) y pass^k por agente (HU-AOS-21).
