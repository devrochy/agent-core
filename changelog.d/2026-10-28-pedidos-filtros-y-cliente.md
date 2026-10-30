## Pedidos: filtros, paginación y enlace al cliente (HU-CRM-20, 2026-10-28)

- `GET /pack/commerce/orders` acepta `estado`, `pago` (pendiente, pagado, rechazado), `q` (número de pedido o nombre del cliente),
  `customer_id`, `limit` y `offset`, y conserva la forma de lista (sin parámetros devuelve lo mismo
  que antes: hasta 200 pedidos). El total de los que coinciden viaja en la cabecera `X-Total-Count`.
  Un `estado` fuera de catálogo responde 400 `invalid_status`.
- `OrderOut` incluye `customer_id` y `customer_name`, para que el panel enlace a la ficha del cliente.
- Ya no se leen todos los ítems y variantes del negocio en cada listado: solo los de la página.
- Migración `crm_0015`: índice `(tenant_id, customer_id)` en `pack_commerce_orders`.
- `GET /admin/escalations`: cada escalación trae `customer_id`, para que el panel abra la ficha del cliente.
- El aviso «Lead nuevo» apunta al cliente (`target.entity = "cliente"`, id del cliente) y no al id del lead: la ficha se abre por cliente, así que el deep link de la APK no podía abrirla. El id del lead sigue en `data.lead_id`.
