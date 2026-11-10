## Confirmación de acciones con efecto (2026-11-09)

- HU-ARCH-36. `confirmar_cotizacion`, `cob_registrar_abono`, `sub_cancelar`, `sub_cambiar_plan` y `cancel_calendar_event` piden el sí del cliente en legacy, en v3 con el motor en sombra y en v3 sin gobierno de etapa, aunque el binding no lo pida.
- Una confirmación solo vale con un mensaje del cliente posterior a la pregunta del turno anterior: el modelo ya no se confirma solo en el mismo turno.
- `confirmation_required` deja de contar como evidencia para el verificador y los guardrails, también en el camino del motor.
- Playbooks de ventas, citas y suscripciones ajustados para una sola pregunta. Suben pack_commerce 3.2.0, pack_calendar 2.4.0 y pack_subscriptions 0.6.0.
