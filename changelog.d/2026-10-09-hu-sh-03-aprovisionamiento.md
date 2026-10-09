## El negocio de quien compra un plan se crea solo (2026-10-09)

- HU-SH-03 (ADR-0120). Cuando un negocio vendedor (`PROVISIONING_SELLER_TENANTS`, en el worker; vacía
  = apagado) activa la suscripción de un plan de la plataforma, el core crea el negocio del comprador:
  plan comprado (Premium activo con el período pagado; Free en prueba de 30 días), preset de su tipo de
  negocio sin datos de ejemplo e invitación al owner por correo y WhatsApp. Sin tokens ni tools para el
  LLM: lo dispara el outbox (`subscription.activated`) y firma la auditoría la identidad de servicio
  `provisioning`.
- Cada alta queda en `provisioning_requests` (migración `0059`, RLS forzada en el vendedor), única por
  referencia del pedido y por suscripción: un evento repetido, un pedido con dos planes o un worker
  que se cae no duplican negocios. Lo que no se puede resolver solo (sin correo, plan desconocido, el
  slug es de otro negocio, el correo es de un master) queda en `revision` con el motivo y un push a
  los dueños del vendedor.
- Invitación con enlace de un solo uso (`tenant_invitations.token_nonce`): una cuenta nueva se crea sin
  contraseña y el owner la define con `POST /api/v1/auth/accept-invitation`; una cuenta existente
  entra y acepta en la app (ADR-0081). Aviso por WhatsApp con el evento `provisioning.invitation`
  (plantillas por evento).
- `pack_commerce`: el pedido de un plan guarda `datos_alta` (correo, teléfono, nombre y tipo de
  negocio; migración `crm_0011`). `checkout_fields` admite `business_name` y `vertical`, y `vertical`
  se valida contra la lista de presets. `subscription.activated` suma `plan_code` y `variant_id`.
