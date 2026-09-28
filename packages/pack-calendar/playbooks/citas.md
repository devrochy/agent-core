---
name: citas
description: Reglas de reagendado y cancelación de citas
---
## Reagendar una cita existente
- Si el cliente pide **mover/reagendar/reprogramar** una cita ya agendada, usá
  SIEMPRE la tool `update_calendar_event` (mueve la cita existente a la nueva
  fecha/hora). **NUNCA uses `create_calendar_event`** para reagendar: eso crea
  una cita NUEVA y deja la anterior, duplicándola.
- Confirmá primero con el cliente la nueva fecha y hora exactas antes de llamar
  la tool (mismo criterio que al crear).

## Cancelar una cita
- Si el cliente pide **cancelar** su cita/demo, usá SIEMPRE la tool
  `cancel_calendar_event` para cancelarla automáticamente (borra el evento de
  Google y la marca cancelada). **NUNCA escales a un humano** ni digas "pasé tu
  solicitud al equipo": cancelá de una y confirmale al cliente.
- Confirmá que el cliente pidió cancelar explícitamente antes de llamar la tool.

## General
- Una cita solo se puede mover/cancelar si ya está agendada; si el cliente pide
  agendar una cita ADICIONAL (nueva), ahí sí usá `create_calendar_event`.
- Si el cliente pregunta "¿puedo reagendar/cancelar?", respondé que sí y pedile
  los datos (nueva fecha/hora o confirmación de cancelación), y luego usá la
  tool correspondiente.