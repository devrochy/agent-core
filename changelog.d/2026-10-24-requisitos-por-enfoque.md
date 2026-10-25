## Avisos de «falta configurar» al crear un agente (2026-10-24)

- HU-AOS-50 entrega 2, ADR-0155. `agent-templates` trae `requirements[]` y nace `GET /api/v1/admin/agents/{id}/requirements`:
  qué le falta al negocio para que el agente funcione (cobro en línea sin habilitar, catálogo, tipos de cita o cartera
  vacíos). Es un aviso: publicar no se bloquea. Sale del hook `contenido_cargado` de cada pack y de las habilidades
  opcionales de la plantilla, sin lógica por habilidad.
