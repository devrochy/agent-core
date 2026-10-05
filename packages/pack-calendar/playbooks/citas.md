---
name: citas
description: Reglas de reagendado y cancelación de citas
---
## Agendar una cita
- **Datos requeridos antes de agendar:** necesitás la **fecha y hora** exactas y el
  **email del cliente**. Si te falta el email, **pedíselo antes de confirmar** (es
  obligatorio: sin email no se puede enviar la confirmación y la tool no crea la cita).
  Si el cliente no lo da, explicá que es necesario para la confirmación.
- **NUNCA inventes el email.** Usá solo el que el cliente escribió en la conversación
  (o el que ya figure en su perfil). Un email inventado no crea la cita y el cliente
  no recibe la confirmación.
- **Si la tool devuelve `needs_data` o un error, NO digas que la cita quedó agendada.**
  Pedile al cliente el dato que falta (p. ej. el email) y, cuando lo dé, volvé a
  llamar la tool. Confirmá recién cuando la tool devuelva `status: "created"`.
- **NUNCA inventes horarios libres.** Antes de proponer cualquier hora, llamá
  `check_availability` con la fecha y ofrecé al cliente **solo** horarios que
  aparezcan en `free_slots`. Si `free_slots` está vacío, decí que no hay
  disponibilidad ese día y proponé otra fecha.
- Si el negocio ofrece varios servicios, usá `list_appointment_types` para
  elegir el tipo correcto (p. ej. una **demo**) y pasá `appointment_type_id` al
  crear; si el cliente pide una duración concreta, pasá `duration_minutes`.
- Creá la cita con `create_calendar_event` **solo** cuando el cliente confirmó
  fecha y hora exactas de la lista de `free_slots`.

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

## Cancelar VARIAS o TODAS las citas
- Si el cliente pide cancelar **varias** o **todas** sus citas (o pregunta
  "¿qué citas tengo?"), usá SIEMPRE `list_client_appointments` para enumerarlas.
- Mostrale al cliente la lista **numerada** (fecha/hora de cada cita) y preguntale
  **cuáles quiere cancelar (por número) o si quiere cancelar todas**.
- Antes de ejecutar, **pedí confirmación explícita** del cliente.
- Luego llamá `cancel_calendar_event` con:
  - `cancel_all: true` si quiere cancelar todas, o
  - `event_ids: [...]` con los `event_id` de la lista para las que eligió
    (si el cliente eligió por NÚMERO, pasá ese número en `event_id`; el sistema
    lo resuelve contra la lista numerada que mostraste).
- Después de llamar la tool, comunicá al cliente **exactamente lo que devolvió**
  (`status: "cancelled"` con los ids cancelados, o `status: "not_found"`).
  **NUNCA afirmes que una cita se canceló si la tool no la devolvió como
  cancelada** — si alguna no se encontró, decilo claramente.

## General
- Una cita solo se puede mover/cancelar si ya está agendada; si el cliente pide
  agendar una cita ADICIONAL (nueva), ahí sí usá `create_calendar_event`.
- Si el cliente pregunta "¿puedo reagendar/cancelar?", respondé que sí y pedile
  los datos (nueva fecha/hora o confirmación de cancelación), y luego usá la
  tool correspondiente.