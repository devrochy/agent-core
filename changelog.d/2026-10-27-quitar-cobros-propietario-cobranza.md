## Se quitan los «Cobros al propietario» de Cobranza (2026-10-27)

- Esa facturación la maneja el agente de suscripciones de solvershub. Se eliminan los endpoints
  `GET/POST /pack/cobranza/owner-charges`, `POST /pack/cobranza/owner-charges/{id}/payment` y
  `POST /pack/cobranza/owner-charges/{id}/links` (con sus schemas `OwnerCharge*`, `OwnerPayment`,
  `PaymentLink*`), las rutas y la plantilla HTML del admin viejo (`/cobranza/cobros*`) y el item «Cobros»
  del menú. **Mergear después de los PRs de panel y APK** (sus checks de schema leen este openapi).
- Se conservan: las tablas `pack_cobranza_cobros_propietario` y `pack_cobranza_links_pago` (los links de
  pago de cuotas y suscripciones siguen usando `LinkPago`), el hilo con el propietario, `core/billing`
  (Wompi y checkout de Suscripciones) y la clave RBAC `pack_cobranza:cobros`, que ahora se declara en
  `section_keys` del módulo porque sigue guardando «Pagos» (`GET /pack/cobranza/payments`).
- Quedan sin consumidor (no se borran): `dias_tolerancia_cobro`, `dias_anticipacion_cobro` y
  `propietario_whatsapp` de la config del pack; el escenario QA `cobranza-completa` sigue sembrando un
  cobro al propietario pendiente (no tiene dónde verse).
