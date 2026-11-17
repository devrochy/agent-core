## Leads en el core, parte 2: el core es el dueño del lead (ADR-0193, HU-AOS-70) (2026-11-17)

- `crear_lead`, `calificar_lead` y `actualizar_lead` son tools del core y llegan solo a los agentes con Captar leads (binding). Ventas, Agenda y Suscripciones nacen con Captar leads; Cobranza no. Un negocio sin comercio ya puede captar leads.
- El lead del cliente se asegura en cada conversación con un agente que tiene Captar leads (reemplaza `auto_create_lead`), con su origen según el canal.
- Clientes: las columnas, filtros, edición y la historia del lead salen del provider del core «Lead»; `PATCH /admin/leads/{id}/pack/pack_commerce` sigue funcionando como alias. Nuevo `POST /api/v1/admin/leads/{id}/seguimiento/hecho` (la ruta de commerce queda como alias).
- Los crons de reenganche y recordatorios de seguimiento pasan al core (`core.leads_follow_up`, `core.leads_recordar_seguimientos`) y solo corren en negocios con la config de Captar leads; la migración 0145 conserva la política guardada por negocio.
- Commerce 4.0.0: sin `LeadSkill`, sin crons de leads y sin modelos de leads (sus tablas quedan, sin escrituras).
