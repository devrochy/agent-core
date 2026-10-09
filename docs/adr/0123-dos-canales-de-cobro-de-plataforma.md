# ADR-0123 — Dos canales de cobro de plataforma con fuente única

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Enmienda:**
  - ADR-0045 § 4: los períodos pagos se cortan por calendario, con día ancla (antes, +30 días).
  - ADR-0046: el panel cobra un **período** con su referencia, no «30 días más»; el webhook de
    billing reenvía lo que no es suyo (riesgo R-05).
  - ADR-0106: una operación **opcional** más del contrato de suscripciones
    (`registrar_pago_externo`), como `permite_varias` en ADR-0111.
  - ADR-0067: una operación opcional de la capacidad de pagos (`procesar_evento`).
  - ADR-0121 § 2: `PROVISIONING_SELLER_TENANTS` va en la **app y en el worker** (antes, solo en el
    worker). La API la usa para reconocer el canal asistente de un negocio (§ 11).
  - ADR-0121 § 3.2: el cambio de plan de un negocio aprovisionado ya no solo se audita, y una
    recompra después de cancelar va a revisión.
  - ADR-0121 § 6: la condición para encender `PROVISIONING_SELLER_TENANTS` en producción pasa a ser
    la lista de requisitos de § 10.
- **Contexto:** HU-SH-04 (SPEC-07, D-13 = A, D-16). Se apoya en ADR-0107 (outbox), ADR-0108 (avisos
  por plantilla), ADR-0110 § 3.1 (preset pendiente) y ADR-0111 (cambio de plan).

## Contexto

- **Dos registros sin sincronizar.** Al pagar el link del asistente, el cargo quedaba pagado en el
  tenant `solvershub` (`pack_subscriptions/module.py:109-192`), pero `tenant_subscriptions` del
  negocio no se enteraba. Al pagar por `/plan`, se extendía `tenant_subscriptions` y el asistente
  seguía cobrando.
- **Sin renovación.** `core/tasks/billing_lifecycle.py:27-68` solo marcaba `past_due`/`expired`, y
  esos estados bloquean en duro (`core/billing/enforcement.py:30`).
- **Períodos desalineados.** El panel sumaba 30 días desde el fin del período o de la prueba
  (`core/billing/checkout.py:136-140`: un Premium desde Free nacía con hasta 60 días, contra D-16).
  El pack corta por calendario. Con +30 días, dos períodos pueden empezar en el mismo mes (1 y 31 de
  enero), así que el mes no servía de referencia.
- **Cambio de plan sin efecto.** Desde el asistente solo se auditaba (ADR-0121 § 3.2) y el preset
  pendiente nunca se aplicaba.
- **`/plan` no dejaba pagar el plan actual.** Solo tenía botones para pasar a otro plan.
- **R-05.** Wompi admite una sola URL de eventos por cuenta y ambiente.

## Decisión

`tenant_subscriptions` del negocio es la **fuente única**. Cada período tiene una referencia
`plat-<slug>-<AAAAMM>`, la misma en los dos canales. Los dos aplican el pago con la misma función,
`aplicar_pago_de_periodo`, que aplica cada período **una sola vez**.

### 1. El período y su referencia

- Se corta **por calendario**: mismo día del mes siguiente; si ese día no existe, el último del mes.
  Un día ancla lo devuelve al 31 en marzo. Es la regla de `pack_subscriptions`, que ahora vive en el
  core (`core/subscriptions/periodos.py`). El pack la re-exporta, así los dos lados cortan el mismo
  día. `tenant_subscriptions.dia_ancla` guarda el ancla.
- La **referencia** es el mes en que empieza el período. Dos períodos seguidos nunca comparten mes.
- `subscription_payments.periodo` guarda la referencia. Un índice único parcial
  `(tenant_id, periodo)` sobre los `aprobado` garantiza en la base que un período se aplica una vez.

### 2. Una sola puerta: `aplicar_pago_de_periodo` (`core/billing/service.py`)

- Toma la suscripción con `FOR UPDATE`: el asistente (worker) y el panel (API) aplican de a uno.
- Es **idempotente** por `provider_reference`: un pago ya procesado no se vuelve a procesar.
- Si el período ya tiene un pago `aprobado` de **otro** pago, este queda `duplicado` (estado nuevo).
  La plata llegó dos veces: se alerta y la suscripción no se toca.
- Los otros intentos pendientes del mismo período pasan a `anulado`.
- **Desde Free, una prueba o una cancelada,** el período pagado reemplaza al de la prueba (D-16).
- **Con un período pago en curso,** extiende `period_end`. `period_start` (la ventana de las
  cuotas) se mueve solo si el período pagado es el que corre hoy.
- **El plan cambia:**
  - en el acto, en un pago del panel o en un cambio de plan del asistente;
  - al empezar el período, en una renovación pagada por adelantado por el asistente (p. ej. con un
    downgrade programado). Lo aplica el cron diario de `billing_lifecycle`.

### 3. Canal asistente → negocio

1. `pack_subscriptions` emite `subscription.charge_paid` por el outbox, en la misma transacción que
   marca pagado el cargo. Trae el período con sus dos bordes, el día ancla y la variante.
2. Un handler del core (`core/billing/canales.py`) lo recibe bajo el `tenant_scope` del vendedor.
   Solo actúa si el vendedor está en la allowlist de ADR-0121.
3. El **vínculo** entre la suscripción y el negocio es la fila del alta (`provisioning_requests`)
   **del vendedor**: el negocio no guarda ids del vendedor. Un evento de otro vendedor no la encuentra.
4. Aplica el período al negocio con `provider = "asistente"` y la referencia como `provider_reference`.
5. El plan sale de la variante (`plan_de_variante` de la fuente de commerce, ADR-0121 § 3.4). Si no
   se puede leer, se toma el plan actual del negocio.

### 4. Canal panel → negocio, y sincronización inversa

- **`start_subscription`** cobra el período que toca (`periodo_a_cobrar`):
  - el vencido que todavía corre;
  - el siguiente, si faltan `BILLING_DIAS_RENOVACION` días o menos;
  - uno nuevo desde hoy, si viene de Free o venció hace más de un período.
- **Plan al día** (el siguiente todavía no se cobra): responde «ya pagado». No se adelantan
  períodos sin límite.
- Antes de crear el link, mira el cargo del mismo período en el vendedor: si el asistente ya lo
  cobró y el evento todavía no llegó al negocio, también responde «ya pagado».
- Un link vivo (menos de 20 h) del mismo período se devuelve otra vez: así no hay dos links.
- **`apply_payment`** (webhook) aplica el período y escribe `platform.period_paid` en el outbox del
  negocio, en la misma transacción.
  - Solo aplica un intento con link del panel: no el cobro de la renovación ni un pago del
    asistente.
  - Si la firma cubre el monto y no es el del link, el pago queda `monto_distinto`, no se aplica y
    se alerta (como `pack_payments`).
- **El handler de la sincronización** relee el pago `aprobado` del negocio y usa su período: no usa
  el del evento.
- **Sincronización inversa.** El handler llama a la operación `registrar_pago_externo` del proveedor
  del vendedor. El cargo del período queda `pagado`, con `pago_externo` (lo crea si no existía), y
  la suscripción se reactiva. El asistente no lo vuelve a cobrar ni a recordar.
- **Link viejo pagado igual.** Si el cliente igual paga el link viejo del asistente:
  - el pack lo reconoce por `pago_externo` y alerta;
  - emite `subscription.charge_paid` con `pago_duplicado`;
  - el core registra el `duplicado` y alerta.

### 5. Sin doble cobro

| Canal | Respuesta |
|---|---|
| Asistente | `charge_link` (`sub_link_pago`) devuelve el error `ya_pagado` |
| Panel | `POST /admin/plan/subscribe` responde 409 `ya_pagado` con la referencia y el canal que lo pagó |
| Base de datos | Índice único de un `aprobado` por período: lo que se cuela es un `duplicado` alertado |

### 6. Cambio de plan

- **Desde el asistente (criterio 7).** Un `subscription.activated` con `cambio_de_plan` sobre una
  suscripción ya aprovisionada:
  - de Free a pago: período pagado desde el cambio, con los bordes del vendedor;
  - entre planes pagos: el plan cambia en el acto (el vendedor cobró la diferencia);
  - en los dos casos se aplica el preset que quedó pendiente (ADR-0110 § 3.1), en el **mismo
    negocio**.
- **Si el alta todavía corre,** primero se termina. Si no se puede, el outbox reintenta.
- **Free vencida.** Quien compra un plan pago después de que venció su Free sigue con su
  suscripción: `crear_suscripcion_para` la reusa. Antes nacía otra suscripción y el alta creaba un
  segundo negocio.
- **Desde el panel.** Un negocio con canal asistente solo paga el período de su plan actual; el
  cambio se pide en el asistente (409 `cambio_en_el_asistente`). Un negocio sin canal asistente
  cambia de plan en `/plan` como antes: plan en el acto y paga el período siguiente.

### 7. Renovación

- **Job diario** (`core.billing_renovacion`, 13:00 UTC, 08:00 en Bogotá). Toma cada plan pago cuyo
  período termina dentro de `BILLING_DIAS_RENOVACION` días (5 por defecto), o que venció hace
  menos de un período:
  - deja el **cobro del período siguiente**: una fila `pendiente` con `provider = "plataforma"` y la
    referencia, sin link. Si alguien paga por el panel, ese intento la usa;
  - escribe `platform.renewal_due` en el outbox;
  - es idempotente: un cobro por período.
- **Aviso por el canal preferido** (handler del evento). El canal preferido es el del último pago:
  - **asistente** (negocio del alta que no pagó por el panel): el cargo del período, su link y su
    aviso `subscription.charge_created` (WhatsApp, plantilla fuera de la ventana de 24 h, ADR-0108)
    los manda el cron `generar_cargos` del vendedor, igual que para cualquier cliente. El handler
    solo confirma con la operación `get` del contrato que esa suscripción puede cobrar;
  - **panel**, o si el asistente no puede cobrar (cancelada, vencida, pausada): correo a los dueños
    con la referencia y el enlace a `/plan`, más un push.
- **La regla vive donde sale el aviso.** `generar_cargo_de` del pack crea el cargo **sin link ni
  aviso** (y `avisos_de_cobro` no lo recuerda) si el último período pagado de esa suscripción tiene
  `pago_externo`: quien paga por el panel no recibe dos cobros del mismo período.
- **Mismo día en los dos lados, por configuración:** `dias_anticipacion` del pack de suscripciones
  del vendedor tiene que ser igual a `BILLING_DIAS_RENOVACION`. La conciliación avisa si difieren
  (`anticipacion_distinta`).
- Un período ya pagado no genera cobro ni aviso.

### 8. Conciliación

- **Job diario** (`core.billing_conciliacion`, 05:30 UTC). Recorre cada vendedor de la allowlist y
  cada negocio que creó.
- **Compara los dos lados,** cada uno con su propio `tenant_scope`:
  - los cargos del vendedor, por la operación `get` del contrato (el core no lee tablas del pack);
  - los pagos por período del negocio.
- **Diferencias que detecta:**
  - pagado en el asistente sin aplicar;
  - pagado en el panel sin sincronizar;
  - pago doble (últimos 7 días);
  - plan distinto;
  - monto distinto;
  - suscripción del vendedor que ya no está, con un plan pago vigente;
  - `dias_anticipacion` del vendedor distinto de `BILLING_DIAS_RENOVACION`.
- **Alerta con el detalle:** `audit_log` del vendedor, un log por diferencia, `send_alert` (Slack si
  está configurado) y un push a los dueños del vendedor.
- No corrige nada solo: cada caso tiene su arreglo en `docs/runbooks/cobro-de-plataforma.md`.

### 9. Riesgo R-05: una URL de eventos por cuenta de Wompi

- **Verificación** (documental, 2026-10-09). La documentación de Wompi («Eventos») dice que la URL
  se configura en el Dashboard de comercios y que «debes configurar una URL de eventos diferente para
  cada ambiente». Hay un solo campo por ambiente. Coincide con lo que registró la skill
  `pagos-wompi` el 2026-10-02. **Riesgo confirmado** si la plataforma y el pack de pagos de
  `solvershub` usan la misma cuenta.
- **Mitigación: un enrutador.**
  - La URL de esa cuenta es `/webhooks/billing/wompi`.
  - El webhook de la plataforma verifica la firma con su secreto de eventos.
  - Si el link no es de un pago de la plataforma y `BILLING_WOMPI_EVENTOS_TENANT` apunta a un
    negocio activo, se lo pasa al pack de pagos de ese negocio (operación opcional
    `procesar_evento`). No depende de la allowlist de vendedores.
  - El pack lo vuelve a verificar con **su** secreto y lo aplica igual que su webhook propio.
  - **Configuración mala, 5xx:** UUID inválido, negocio inactivo, sin pack de pagos o con otro
    secreto. Wompi reintenta (hasta 3 veces en 24 h) mientras se corrige; un 200 perdería el pago.
- **Cuentas separadas:** la variable queda vacía y cada cuenta tiene su URL.
- **Falta verificarlo en el sandbox real:** campo único de URL en el dashboard y un pago de cada
  canal.

### 10. Requisitos para encender `PROVISIONING_SELLER_TENANTS` en producción

Reemplazan la condición de ADR-0121 § 6. No se enciende hasta cumplir todos:

1. **El canal preferido se respeta donde sale el aviso** (§ 7): el cron del vendedor no le manda
   link ni aviso a quien pagó el último período por el panel, y `dias_anticipacion` del vendedor
   es igual a `BILLING_DIAS_RENOVACION` (sin `anticipacion_distinta` en la conciliación).
2. **`PROVISIONING_SELLER_TENANTS` está en la app y en el worker** (§ 11), y
   `BILLING_WOMPI_EVENTOS_TENANT` está configurada según la cuenta de Wompi (§ 9).
3. **R-05 verificado en el sandbox de Wompi:** un solo campo de URL de eventos por ambiente, la URL
   apuntando donde corresponde y un pago de cada canal con su efecto (runbook
   `docs/runbooks/cobro-de-plataforma.md` § 5).
4. **Los criterios de HU-SH-04 verificados a mano en test.**
5. **Los controles de HU-SH-07** (ADR-0125): términos, privacidad y DPA publicados por el master
   en Plataforma (con el texto del abogado, HU-GTM-02), el playbook de ventas actualizado
   (`terminos_pendientes`, `free_no_disponible`, `limite_de_altas`) y los criterios de HU-SH-07
   verificados a mano en test.

### 11. `PROVISIONING_SELLER_TENANTS` en la app y en el worker

La API también la lee: con ella sabe que un negocio tiene canal asistente. Sin la variable en la
app, `/plan` no responde 409 `cambio_en_el_asistente` ni «ya pagado en el asistente», y muestra los
botones de cambio de plan. El enrutador de R-05 no depende de ella (§ 9).

### 12. Cierres de dinero de la revisión (2026-10-09)

- **Plan por menos de su precio.** Un cargo pagado o un cambio de plan del asistente por menos que
  el precio del plan de la plataforma no se aplica: queda un pago `monto_distinto` y se alerta. Pasa,
  por ejemplo, con una suscripción creada a mano con otro precio. La alta manual de suscripciones del
  pack exige además permiso de escritura.
- **Monto del panel firmado.** El webhook del panel solo aplica si Wompi firmó el monto y es el del
  link; si no, `monto_distinto` y alerta (como `pack_payments`).
- **Candados en el mismo orden.** `apply_payment` bloquea primero la suscripción y después el pago,
  igual que el canal asistente.
- **Link viejo de otro plan.** Al cambiar el plan se anulan los links pendientes de otro plan. Un
  intento anulado que igual se paga queda como pago doble: no devuelve el negocio al plan viejo.
- **Recompra después de cancelar.** El mismo cliente con un negocio de un alta anterior va a
  revisión, en vez de recibir un segundo negocio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Que el panel cobre con el link del asistente (un solo riel) | D-13 y el diseño de SPEC-07 piden el panel con `core/billing`; además, el panel de un negocio dependería de la config de pagos del vendedor |
| Seguir con +30 días y la referencia por fecha de pago | Dos períodos en el mismo mes y lados desalineados: la referencia deja de identificar un período |
| Tabla nueva de períodos | Una columna `periodo` y un índice único alcanzan; `subscription_payments` ya era el libro de pagos |
| Candado o ventana de tiempo para evitar el pago doble | La garantía es el índice único en la base; lo que se cuela es plata real: se registra `duplicado` y se alerta |
| Abonar el pago doble al período siguiente en forma automática | Es una decisión de dinero que el cliente no tomó: la toma una persona con el runbook |
| Que el asistente consulte al core antes de cada link | Un pack leyendo el plan de otro negocio; la sincronización inversa por el outbox y el pago doble alertado alcanzan |
| Sincronizar al asistente un cambio de plan hecho en el panel | Exige cambiar el plan en el vendedor sin cobrarlo (otra operación del contrato) y el prorrateo es de HU-BILL-12 |
| Cuentas de Wompi separadas como única opción | Exige una segunda cuenta de comercio para la misma empresa; queda como alternativa (variable vacía) |
| Enrutar desde el webhook del negocio hacia la plataforma | Si el negocio vendedor se suspende o rompe su config de pagos, la plataforma pierde sus eventos |
| Mantener la «renovación anticipada» ilimitada del panel | Permitía pagar meses por adelantado y cobrar en el panel un período que el asistente ya tenía pendiente |

## Consecuencias

**Positivas:**
- el cliente paga por donde quiera y el negocio queda al día por los dos caminos;
- un período nunca se aplica dos veces, y la plata que llega dos veces se ve el mismo día;
- el plan pago que crea el alta se renueva y se desbloquea al pagar;
- un Free que sube de plan sigue en su negocio, con el preset de su vertical;
- con una cuenta de Wompi compartida, ningún evento se pierde.

**Negativas / lo que aceptamos pagar:**
- **Más días en algunos períodos.** El primer período del alta y el del panel pasan de 30 días a un
  mes (28 a 31 días). Es coherente con el precio «por mes» y con el pack.
- **Cambio de plan en el panel.** Un negocio del asistente no puede cambiar de plan desde `/plan`
  hasta HU-BILL-12.
- **El pago doble lo resuelve una persona** (devolver o abonar).
- **«Canal preferido» se deduce del último pago.** No hay una preferencia explícita en el panel.
- **Se agrega un paso** en la ruta del dinero (`platform.period_paid`). Si falla, el asistente
  podría recordar un período pagado: lo muestra la conciliación del día.
- **Recompra después de cancelar.** Un cliente que canceló y vuelve a comprar sigue creando otro
  negocio (fuera de esta HU).
- **El bloqueo sigue siendo duro** al vencer `period_end` (ADR-0045). El aviso llega 5 días antes y
  el pago desbloquea en el acto.

**Qué haría que revisáramos esto:**
- más de un pago doble por mes (habría que impedir los dos links vivos del mismo período);
- diferencias de conciliación que se repiten varios días seguidos (falla un camino del outbox);
- HU-BILL-12 (cambio de plan con prorrateo y planes anuales);
- un segundo vendedor con reglas distintas;
- que Wompi permita varias URL de eventos por cuenta.

## Impacto en el código

- **Calendario:** `core/subscriptions/periodos.py` (antes en el pack; `pack_subscriptions/periodos.py`
  lo re-exporta) y `core/billing/periodos.py` (`Periodo`, referencia y `periodo_a_cobrar`).
- **`core/billing/`:**
  - `service.py`: `aplicar_pago_de_periodo`; `registrar_periodo_pagado` pasa a usarla;
  - `checkout.py`: período del panel, `PeriodoYaPagado`, `CambioDePlanEnElAsistente`,
    `tenant_del_pago` y `platform.period_paid`;
  - `canales.py`: vínculo con el vendedor, handlers de los dos canales y alerta de pago doble;
  - `renovacion.py`, `conciliacion.py` y `enrutador.py` (R-05).
- **Tareas, eventos y config:**
  - `core/tasks/billing_lifecycle.py`: paso al período pagado por adelantado;
  - `core/tasks/celery_app.py`: `core.billing_renovacion` y `core.billing_conciliacion`;
  - `core/events/core_handlers.py` y `core/shared/events.py`: `subscription.charge_paid`,
    `platform.period_paid` y `platform.renewal_due`;
  - `core/config.py`: `BILLING_DIAS_RENOVACION` y `BILLING_WOMPI_EVENTOS_TENANT`;
  - `core/gateway/webhooks.py`: reenvío de R-05.
- **Contratos:**
  - `core/subscriptions/__init__.py`: helpers de las operaciones opcionales;
  - `core/provisioning/fuentes.py`: `plan_de_la_variante`.
- **Alta automática:** `core/provisioning/consumer.py`, con el primer período alineado con el
  vendedor, el cambio de plan y el preset pendiente.
- **API:** `core/api/routers/plan.py` y `core/admin/plan_view.py` (`cobro`, `periodo` y los 409);
  `docs/openapi.json`.
- **Migraciones:**
  - `alembic/versions/0061_cobro_de_plataforma_dos_canales.py`: `subscription_payments.periodo` con
    su índice único, `tenant_subscriptions.dia_ancla` e índice del negocio en
    `provisioning_requests`;
  - `pack_subscriptions_0007`: `pack_subscriptions_charges.pago_externo`.
- **Packs:**
  - `pack_subscriptions`: `subscription.charge_paid`; `registrar_pago_externo`; sin link ni aviso a
    quien paga por otro canal; alta manual con permiso de escritura;
    Free vencida reusada; el período en `subscription.activated`; `generar_cargo_de`;
  - `pack_commerce`: `plan_de_variante`;
  - `pack_payments`: `procesar_evento`.
- **Documentación:** `docs/BILLING.md`, `docs/despliegue-coolify.md` y
  `docs/runbooks/cobro-de-plataforma.md`.

## Enmienda (2026-10-09): el aviso de renovación sale por el SMTP del vendedor

**Contexto.** El aviso de renovación por el panel salía por el SMTP de la plataforma
(`SYSTEM_SMTP_*`). Pero lo manda el **vendedor** (el negocio que vende planes, por ejemplo
`solvershub`) a su cliente: es comunicación comercial suya. Con varios vendedores
(`PROVISIONING_SELLER_TENANTS` es una lista), el cliente de una agencia recibiría el cobro con el
remitente de SolversHub.

**Decisión.** Cada correo sale por donde le corresponde:

| Correo | Por dónde | Por qué |
|---|---|---|
| Aviso de renovación (canal panel) de un negocio del alta automática | Integración SMTP del **vendedor** (canal de correo saliente, ADR-0071): queda como una conversación de correo del vendedor con ese cliente | Es comunicación comercial del vendedor: su marca y su remitente. No lleva secretos (solo el enlace a `/plan`) |
| El mismo aviso, si el vendedor no tiene una integración de correo activa, o el negocio no tiene vendedor | SMTP de la plataforma (`SYSTEM_SMTP_*`, ADR-0098) | Respaldo: el aviso no se pierde |
| Acceso del alta (enlace que define la contraseña, ADR-0121) | SMTP de la plataforma (sin cambio) | Es seguridad de la cuenta, que vive en la plataforma. Por el canal del vendedor, el enlace quedaría guardado en sus conversaciones y lo verían sus colaboradores (el mismo problema que se cerró al sacarlo de WhatsApp) |
| Recuperación de contraseña | SMTP de la plataforma (sin cambio) | No hay negocio en contexto (ADR-0098) |

`renovacion_avisada` registra el `remitente` (`vendedor` o `plataforma`).

**Fuera de esta enmienda.** Un negocio puede crear varias integraciones SMTP, pero al enviar se usa
una sola: la principal o la más nueva (`core/gateway/connections.py:123-140`). Elegir el SMTP por
propósito (ventas, facturación…) es parte de las integraciones de Agent OS (ADR-0115) y queda en
`docs/BACKLOG.md`.

**Impacto en el código.** `core/billing/renovacion.py` (`_enviar_aviso`) y sus tests en
`tests/integration/test_cobro_plataforma.py`.

