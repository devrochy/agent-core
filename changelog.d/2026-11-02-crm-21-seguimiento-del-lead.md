## Próxima acción y fecha de seguimiento del lead (HU-CRM-21, 2026-11-02)

- **Seguimiento del lead.** Una persona deja anotado qué va a hacer con un lead y cuándo (`next_action_note`,
  `next_action_at`, migración `crm_0018`). La fecha se guarda en UTC; lo que se escribe sin zona es hora del
  negocio y «hoy» se cuenta con la zona del negocio, no con la del servidor.
- **Contrato.** Nuevo tipo `fecha` en los campos editables de la ficha (ADR-0149, enmienda ADR-0181); el campo
  `seguimiento_en` se devuelve en hora del negocio para el selector. La lista trae `seguimiento_en`,
  `seguimiento_nota` y `seguimiento_estado`; filtro `seguimiento` (`vencidos`, `hoy`, `proximos_7_dias`,
  `mas_adelante`, `sin_seguimiento`, con contadores por faceta) y `orden=seguimiento` (el más atrasado primero).
- **Historia.** Tabla nueva `pack_commerce_lead_followups`: al reprogramar, quitar o completar un seguimiento queda
  su nota y la fecha que tenía en la línea de tiempo del cliente. Ruta nueva
  `POST /api/v1/pack/commerce/leads/{cliente}/seguimiento/hecho` para completarlo y programar el siguiente.
- Programar no mueve `last_activity_at` y el agente no tiene cómo cambiarlo (sin tools nuevas ni cambios de prompts).
