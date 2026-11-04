## `GET /admin/dashboard/agents` trae la serie diaria del negocio entero (2026-11-03)

- ADR-0171, rediseño del Resumen (SPEC-29). `general` suma el campo `serie` (turnos, conversaciones y costo por día, cero-relleno,
  `dias` puntos): la suma de todos los agentes, el Coordinador y lo no atribuido, así que coincide con el total del negocio. La
  serie del agente «General» sigue siendo solo su actividad más lo no atribuido.
- Campo opcional con valor por defecto `[]`; vacío sin la sección `agents` (igual que la serie por agente) y con `costo` nulo
  sin la sección `costs`. Sin cambios de esquema de base de datos.
