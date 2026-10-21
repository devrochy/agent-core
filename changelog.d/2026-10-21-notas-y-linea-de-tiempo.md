## Clientes: notas del equipo y línea de tiempo (HU-CRM-04, ADR-0147)

- Notas por cliente (`GET/POST /api/v1/admin/leads/{id}/notes`, `DELETE …/notes/{note_id}`), en la tabla
  nueva `customer_notes` (migración 0099, RLS forzada, borrado lógico). Borra su autor, el propietario o
  un admin de plataforma. La purga de memoria del cliente las olvida.
- `GET /api/v1/admin/leads/{id}/timeline`: conversaciones, escalaciones, datos aprendidos (también los
  reemplazados), notas, cotizaciones y pedidos (pack_commerce) y citas (pack_calendar), del más nuevo al
  más viejo, con cursor compuesto y `partial` si un pack falla.
- `LeadProvider` suma `timeline` (enmienda de ADR-0056); `resolver` pasa a ser opcional.
- El agente ve las últimas notas del cliente en el bloque dinámico (rótulos en el playbook del kernel
  `notas-del-equipo`, texto escapado como dato). Sin notas el prompt no cambia.
- Evals: escenario golden 36 (la nota se respeta y la orden plantada en ella no), sumado al gate.
- Índice `escalations (tenant_id, conversation_id, created_at)`.
