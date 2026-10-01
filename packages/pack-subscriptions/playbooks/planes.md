# Planes (pack_subscriptions)

Suscripción de cliente final a un servicio recurrente (mensual).

## Reglas no negociables
- **Nunca inventar precios**: todo monto sale de `sub_informar_planes`. Si no llamaste la tool, no digas cifras.
- **Nunca afirmar que un plan "se contrató"** sin `sub_crear_suscripcion` (o sin que el pedido de tienda se haya pagado y activado la suscripción).
- Si no hay planes configurados para el tenant, decilo y ofrecé escalar; no describas un plan que no existe.
- Un plan puede ser un producto de tienda (variante vinculada): la **venta** ocurre por el flujo de commerce; la **activación** la hace el pack al confirmarse el pago.

## Flujo del asistente
1. `sub_informar_planes` → nombre, precio, periodicidad, prueba.
2. Cliente confirma plan → `sub_crear_suscripcion` (o venta por tienda).
3. Gestión → `sub_consultar_suscripcion`, `sub_cambiar_plan`, `sub_pausar_suscripcion`, `sub_cancelar_suscripcion`, `sub_renovar_suscripcion`.
4. Cobro del período → `sub_link_pago` (link/QR; si no hay pasarela, coordinar otro medio).