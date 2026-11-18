## Comprobantes: la hora se compara en la zona del negocio (2026-11-17, HU-AI-01, HU-COM-24)

- `comprobar_comprobante` pasa la hora del pedido a la zona del negocio antes de compararla con la del comprobante, que el banco imprime en hora local y sin zona. Con UTC, un pago hecho minutos después del pedido parecía cinco horas anterior y quedaba siempre «en revisión» (visto en el ciclo de pruebas en test).
