## El negocio de quien compra un plan se crea solo (2026-10-09)

- HU-SH-03 (ADR-0121). Cuando un negocio vendedor (`PROVISIONING_SELLER_TENANTS`, UUID, en el worker;
  vacía = apagado; **no encenderla en producción antes de HU-SH-04**) activa la suscripción de un plan
  de la plataforma, el core crea el negocio del comprador:
  - con el plan comprado: Premium activo con el período pagado y su pago en `subscription_payments`;
    Free en prueba de 30 días;
  - con el preset de su tipo de negocio, sin datos de ejemplo;
  - con una invitación al owner.

  No hay tokens ni tools para el LLM: lo dispara el outbox (`subscription.activated`) y firma la
  auditoría la identidad de servicio `provisioning`.
- Cada alta queda en `provisioning_requests` (migración `0059`, RLS forzada en el vendedor), única por
  referencia del pedido y por suscripción: un evento repetido, un pedido con dos planes o un worker que
  se cae no duplican negocios.
- Queda en `revision`, con el motivo y un push a los dueños del vendedor, lo que no se puede resolver
  solo: sin correo, plan desconocido, slug ajeno, correo de un master, cliente distinto o un Free repetido
  del mismo cliente o correo. El master la reintenta o la descarta con
  `agent-core tenant reintentar-alta | descartar-alta` (`docs/altas-en-revision.md`).
- El enlace de un solo uso (`tenant_invitations.token_nonce`) va **solo por correo** y solo a una cuenta
  que el alta creó sin contraseña. El owner la define con `POST /api/v1/auth/accept-invitation`, que tiene
  bloqueo progresivo y solo sirve para una cuenta limpia. Una cuenta existente entra y acepta en la app.
- Por WhatsApp sale siempre el mismo aviso, sin token y con el correo enmascarado
  (`provisioning.invitation`). Aceptar una invitación vencida en la app ya no da acceso.
- `onboard_tenant` pasa a `core/alta_negocio.py`, y el contrato `alta-fuera-del-llm` de import-linter se
  lo prohíbe a las tools y a los packs.
- `pack_commerce`:
  - el pedido de un plan guarda `datos_alta` (migración `crm_0011`): correo, teléfono, nombre y tipo de
    negocio;
  - `checkout_fields` admite `business_name` y `vertical`, que pide `missing_fields` (no van en el schema
    de la tool). `vertical` se valida contra la lista de presets, y un nombre con enlaces se rechaza;
  - un plan va en su propio pedido: `agregar_item_pedido` ya no le suma productos.
- `subscription.activated` suma `variant_id` y manda `referencia` nula cuando no hubo pedido.
