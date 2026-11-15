## Prueba del panel: la cotización simulada ya no apaga Comercio (2026-11-15)

- HU-ARCH-14: `simular_generar_cotizacion` y `simular_agregar_item_pedido` leían `data["sin_inventario"]`, que `_get_variant_with_stock` nunca devuelve. Cada cotización en la prueba del panel lanzaba `KeyError` y ADR-0024 apagaba `pack_commerce` para el negocio («el agente no puede acceder al inventario»). El simulador valida el stock igual que el handler real. Test de regresión en `tests/test_sandbox_comercio_stock.py`.
