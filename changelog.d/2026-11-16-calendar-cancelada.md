## El chequeo de citas no confunde cancelaciones de otros módulos (2026-11-16)

- `calendar_completion_check` (pack-calendar) ya no exige `cancel_calendar_event` cuando la cancelación es de otro módulo («tu suscripción quedó cancelada», «el pedido fue cancelado»): la afirmación solo cuenta si en la oración no hay un sujeto de suscripciones, pedidos o cobros. Las frases sin sujeto («ya quedó cancelada») siguen exigiendo la tool (D-76).
