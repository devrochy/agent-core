## El plan de SolversHub se paga por el asistente o por el panel, sin cobrar dos veces (2026-10-09)

- HU-SH-04 (ADR-0123). `tenant_subscriptions` del negocio es la fuente única del cobro de la
  plataforma. Cada período tiene la referencia `plat-<slug>-<AAAAMM>`, la misma en los dos canales,
  y se aplica una sola vez:
  - `aplicar_pago_de_periodo` es la única puerta;
  - un índice único de `subscription_payments` admite un pago `aprobado` por período;
  - un segundo pago del mismo período queda `duplicado` y se alerta.
- Los períodos pagos se cortan **por calendario** (mismo día del mes siguiente, con día ancla), igual
  que `pack_subscriptions`: el primer período del alta y el del panel duran un mes, no 30 días. La
  regla vive en `core/subscriptions/periodos.py`.
- **Canal asistente:** al pagarse el cargo de un período, `pack_subscriptions` emite
  `subscription.charge_paid` y el core extiende el plan del negocio aprovisionado.
- **Canal panel:**
  - `/plan` cobra el período que toca, con su referencia;
  - al pagarse, `platform.period_paid` deja pagado el cargo del mismo período en el asistente
    (sincronización inversa): el asistente no lo vuelve a cobrar ni a recordar;
  - `GET /api/v1/admin/plan` trae `cobro` y `plan_por_el_asistente`, y los pagos traen `periodo`.
- **«Ya pagado» en los dos canales:**
  - `sub_link_pago` devuelve `ya_pagado`;
  - `POST /api/v1/admin/plan/subscribe` responde 409 `ya_pagado` y no adelanta períodos con el plan al
    día;
  - un negocio del asistente cambia de plan en el asistente (409 `cambio_en_el_asistente`).
- **Renovación** (`core.billing_renovacion`, diaria, 13:00 UTC): 5 días antes del fin del período
  (`BILLING_DIAS_RENOVACION`) genera el cobro del siguiente y avisa por el canal preferido:
  - por WhatsApp: el cargo y su aviso con la plantilla de `subscription.charge_created` fuera de
    24 h los manda el cron del vendedor (`dias_anticipacion` igual a `BILLING_DIAS_RENOVACION`), que
    ya no le manda link a quien pagó el último período por el panel;
  - o por correo a los dueños, con el enlace a `/plan`.
- **Conciliación** (`core.billing_conciliacion`, diaria): compara el asistente con el negocio y alerta
  cada diferencia con su detalle: auditoría, Slack y push. Qué hacer con cada una:
  `docs/runbooks/cobro-de-plataforma.md`.
- **Cambio de plan desde el asistente:** un Free del alta que compra un plan pago cambia de plan en el
  mismo negocio y recibe el preset que había quedado pendiente. Comprar después de que venció la prueba
  ya no crea otro negocio: la suscripción Free vencida se reusa.
- **Riesgo R-05 verificado:** Wompi admite una sola URL de eventos por cuenta y ambiente. Con una cuenta
  compartida, `/webhooks/billing/wompi` reenvía al negocio de `BILLING_WOMPI_EVENTOS_TENANT` los eventos
  que no son de la plataforma; su pack de pagos los verifica con su propio secreto.
- **Migraciones:**
  - `0061`: `subscription_payments.periodo` con su índice único, `tenant_subscriptions.dia_ancla` e
    índice del negocio en `provisioning_requests`;
  - `pack_subscriptions_0007`: `pack_subscriptions_charges.pago_externo`.
- **Cierres de dinero** (revisión de seguridad y arquitectura):
  - un cargo o un cambio de plan del asistente por menos que el precio del plan no se aplica
    (`monto_distinto` + alerta);
  - la alta manual de suscripciones del pack exige permiso de escritura;
  - el webhook del panel exige el monto firmado por Wompi;
  - al cambiar de plan se anulan los links pendientes del plan anterior;
  - una recompra después de cancelar va a revisión.
- `PROVISIONING_SELLER_TENANTS` va en la **app y en el worker**. Los requisitos para encenderla en
  producción están en ADR-0123 § 10.
- Un test exige un solo head de Alembic en el core y en cada pack.
