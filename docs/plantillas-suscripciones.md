# Plantillas de WhatsApp para suscripciones (HU-SUB-05)

Los avisos de cobro salen como **texto** si el cliente escribió en las últimas 24 h y como
**plantilla aprobada** si no (ADR-0108). El texto es la config del pack
(`pack_subscriptions`: `aviso_cargo_texto`, `recordatorio_texto`, `aviso_pausa_texto`), editable por el
negocio. La plantilla la crea el negocio en Meta (categoría **Utilidad**) y la mapea en
**Plantillas por evento** del panel. Variables: solo rutas permitidas del evento, nunca texto del LLM.

Sugerencias (español, categoría Utilidad; el `{{n}}` es la variable posicional que se mapea):

| Evento | Nombre sugerido | Cuerpo | Variables (`{{1}}`, `{{2}}`…) |
|---|---|---|---|
| `subscription.charge_created` | `aviso_cobro_suscripcion` | Hola {{1}}, ya está el cobro de tu plan {{2}} por {{3}}, con vencimiento el {{4}}. Puedes pagarlo en este link: {{5}} | `cliente.nombre`, `suscripcion.concepto`, `cargo.monto`, `cargo.fecha_vencimiento`, `cargo.link` |
| `subscription.charge_reminder` | `recordatorio_pago_suscripcion` | Hola {{1}}, te recordamos el pago de tu plan {{2}} por {{3}}, que vence {{4}}. Link de pago: {{5}} | `cliente.nombre`, `suscripcion.concepto`, `cargo.monto`, `recordatorio.dias`, `cargo.link` |
| `subscription.paused` | `suscripcion_pausada_mora` | Hola {{1}}, pausamos tu plan {{2}} porque el pago de {{3}} lleva {{4}} días pendiente. Cuando pagues se reactiva: {{5}} | `cliente.nombre`, `suscripcion.concepto`, `cargo.monto`, `mora.dias`, `cargo.link` |

Notas:

- Meta rechaza plantillas de **utilidad** que suenan a marketing: sin promociones ni emojis de venta.
- El link va en el **cuerpo** (v1 no resuelve variables de botones, ADR-0108).
- Cada plantilla enviada consume el fee de plataforma (0,3 créditos) y Meta la cobra aparte.
- Cuándo sale cada aviso: el cargo se genera `dias_anticipacion` antes del vencimiento (3 por defecto) con
  su link; los recordatorios en `recordatorios_dias` (−3, 0, +3, +7 por defecto; el −3 coincide con el aviso
  del cargo y no se repite); la pausa, si `pausar_tras_dias_mora` > 0. Un cargo pagado no recibe nada.
- Qué se envió por cada cargo: `GET /api/v1/pack/pack_subscriptions/cargos/{id}/avisos`.
