## Escenarios de pago en línea y transferencia (2026-11-17, HU-COM-23, HU-COM-24)

- Set de journeys `pagos` (4 escenarios grabados con LLM real) en el gate en replay del CI: reenviar el link, estado del pago en línea, «ya transferí» y «¿les llegó mi transferencia?».
- Verificador: «link de pago» solo cuenta como afirmación si anuncia o presenta un link; nombrar la forma de pago ya no escala la venta.
- `verificar_pago_pedido`: la pista exige `confirmar_pago_pedido` para reenviar el link (el modelo decía «te lo reenvío» sin enviarlo).
- Salidas estables para el replay: `existing_data` de `crear_pedido` y `updated` de `update_customer_profile` van ordenados.
