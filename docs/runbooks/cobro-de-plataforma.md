# Cobro de la plataforma por dos canales (runbook)

El plan de un negocio se paga por dos canales (HU-SH-04, ADR-0123):

- **asistente:** el link que manda el asistente de `solvershub` (cargo de la suscripción del cliente
  en el negocio vendedor);
- **panel:** «Plan» del negocio (`/plan`, link de la plataforma).

Los dos actualizan la **misma** `tenant_subscriptions` del negocio. Cada período tiene la referencia
`plat-<slug>-<AAAAMM>`, la misma en los dos lados.

## 1. Dónde mirar

| Qué | Dónde |
|---|---|
| Períodos pagados del negocio | `subscription_payments` del negocio (`periodo`, `status`, `provider`: `asistente` o `wompi`) |
| Cargos del asistente | Suscripciones del vendedor (`pack_subscriptions_charges`, `pago_externo` si lo pagó el panel) |
| Lo que hizo el core | `audit_log` con actor `billing` |
| Eventos entre los dos lados | Bandeja de fallos del outbox: `subscription.charge_paid` y `subscription.activated` en el vendedor; `platform.period_paid` y `platform.renewal_due` en el negocio |

Acciones de `audit_log` con actor `billing`:

- `cobro_plataforma_aplicado`: período pagado por el asistente, aplicado al negocio;
- `cobro_plataforma_sincronizado`: período pagado por el panel, registrado en el vendedor;
- `cobro_plataforma_pago_duplicado`: pago doble;
- `renovacion_avisada`: aviso de la renovación y canal;
- `conciliacion_cobro_plataforma`: diferencias del día, con el detalle.

## 2. Alertas

- **«Pago doble del plan de un negocio»** (crítica, Slack y push al vendedor): un período se pagó
  por los dos canales. El negocio quedó extendido **una** vez; el segundo pago está como
  `duplicado` en `subscription_payments`.
- **«Conciliación del cobro de la plataforma: N diferencia(s)»** (Slack y push): el detalle está en
  `audit_log` (`conciliacion_cobro_plataforma`) y en los logs `billing_conciliacion_diferencia`.

## 3. Qué hacer con cada diferencia

| Diferencia | Qué pasó | Qué hacer |
|---|---|---|
| `pagado_en_el_asistente_sin_aplicar` | El cliente pagó el link del asistente y el negocio no se enteró | En la Bandeja de fallos del **vendedor**, reintentar el `subscription.charge_paid` de esa suscripción. Si no está, extender el período desde Plataforma (override del master) y anotar la referencia |
| `pagado_en_el_panel_sin_sincronizar` | Pagó por el panel y el asistente lo sigue cobrando | En la Bandeja de fallos del **negocio**, reintentar `platform.period_paid`. Mientras tanto, el asistente puede recordar el cargo: avisarle al cliente que ya está pago |
| `pago_duplicado` | La plata llegó dos veces por el mismo período | Decidir con el cliente: **devolver** el segundo pago desde el dashboard de Wompi, o **abonarlo** extendiendo un período desde Plataforma. Se deja de reportar a los 7 días |
| `plan_distinto` | El plan del asistente no es el del negocio (un cambio de plan que no se aplicó) | Reintentar el `subscription.activated` del vendedor (Bandeja de fallos). Si no aplica, corregir el plan desde Plataforma |
| `monto_distinto` | El cargo pendiente del asistente no cobra el precio del plan | Revisar la variante del plan en el catálogo del vendedor (`precio_de_plan`) y el cron `sincronizar_precios_de_plan` |
| `sin_suscripcion_en_el_asistente` | El cliente canceló en el asistente y el negocio sigue con plan pago | Nada, salvo que el cliente quiera seguir: paga por el panel. Al terminar el período, el negocio pasa a `past_due` |
| `no_se_pudo_conciliar` | Falló la lectura de un lado | Ver el log `billing_conciliacion_negocio_fallo` |
| `anticipacion_distinta` | El asistente cobra el período siguiente otro día que el panel | Poner `dias_anticipacion` de la config de Suscripciones del vendedor igual a `BILLING_DIAS_RENOVACION` |

Otras alertas:

- **«Plan cobrado por menos que su precio»** (crítica): un cargo o un cambio de plan del asistente
  por menos que el precio del plan. No se aplicó; queda un pago `monto_distinto` en el negocio.
  Revisar la suscripción en el vendedor (¿se creó a mano con otro precio?).
- **«Pago del plan con monto distinto»** (crítica): el webhook del panel trajo otro monto o no lo
  firmó. Revisar la transacción en Wompi.
- **Recompra en revisión:** un cliente que canceló y volvió a comprar no recibe un segundo
  negocio; el alta queda en revisión (`docs/runbooks/altas-en-revision.md`). Lo normal es
  reactivar el negocio que tenía y descartar el alta nueva.

Ningún arreglo automático toca plata: la conciliación solo avisa.

## 4. Renovación

- **Cuándo corre.** Todos los días a las 13:00 UTC (`core.billing_renovacion`), para cada plan pago
  cuyo período termina dentro de `BILLING_DIAS_RENOVACION` días (5).
- **Qué deja.** Un cobro `pendiente` con `provider = "plataforma"` y la referencia del período.
- **Por dónde avisa.**
  - Al negocio del alta que paga por WhatsApp: el cargo, el link y el aviso los manda el cron
    `generar_cargos` del vendedor (`dias_anticipacion` = `BILLING_DIAS_RENOVACION`), con
    plantilla fuera de 24 h. A quien pagó el último período por el panel, el cron no le manda link
    ni recordatorios.
    Hay que tener mapeada la plantilla de `subscription.charge_created` en el vendedor (Plantillas
    por evento, ADR-0108). Si no, el aviso queda `sin_plantilla` y el panel lo muestra.
  - Al resto: correo a los dueños con el enlace a `/plan`. Hace falta `SYSTEM_SMTP_*`; sin él solo
    sale el push.

## 5. Una cuenta de Wompi compartida (riesgo R-05)

Wompi admite **una** URL de eventos por cuenta y por ambiente. Si la plataforma (`WOMPI_BILLING_*`) y
el pack de pagos de `solvershub` usan la **misma** cuenta:

1. En el dashboard de Wompi de esa cuenta, en sandbox y en producción, configurar la URL de eventos
   `https://<host>/webhooks/billing/wompi`.
2. Poner `BILLING_WOMPI_EVENTOS_TENANT=<UUID de solvershub>` en la app (es la que recibe el webhook).
   Si el reenvío no se puede hacer, el webhook responde 500 y Wompi reintenta: corregir y esperar
   el reintento (hasta 3 en 24 h).
3. El secreto de eventos del pack de pagos de `solvershub` tiene que ser el de esa cuenta: es el mismo
   que `WOMPI_BILLING_EVENTS_SECRET`.

Con **cuentas separadas**, cada cuenta apunta a su URL (la de la plataforma a `/webhooks/billing/wompi`,
la de `solvershub` a `/webhooks/payments/wompi/solvershub`) y la variable queda vacía.

**Verificación en el sandbox:**

1. Pagar un pedido de `solvershub` con el link del asistente: la transacción del pack queda `aprobado`
   y el log muestra `billing_webhook_reenviado`.
2. Pagar un período desde `/plan` de un negocio: `subscription_payments` queda `aprobado`.
