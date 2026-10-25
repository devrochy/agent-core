## El mínimo del pago en línea lo declara la pasarela según su ambiente (HU-SUB-13, adenda de ADR-0067) (2026-10-25)

El 150.000 COP de Wompi es el mínimo del **sandbox**; en producción es 1.500. El gate de `crear_pedido` lo tenía como default de `pack_commerce` y, en producción, rechazaba un plan de 80.000 pagado en línea salvo que el negocio lo bajara a mano.

- `core.payments.monto_minimo(capacidad)`: operación opcional de la capacidad (molde de `procesar_evento`). `WompiPaymentCapability.monto_minimo()` devuelve 150.000 en sandbox y 1.500 en producción.
- `pack_commerce`: `pago_en_linea_monto_minimo` pasa a `None` por defecto (usa el de la pasarela). Un valor explícito manda, y 0 apaga el gate (antes `or 150_000` lo volvía a prender). Sin pasarela que lo declare, queda 150.000.
- Playbook de ventas sin la cifra: `pack_commerce` **2.5.0** y lock regenerado. Cambia el prompt: hay que **regrabar los cassettes del gate** (lo hace el dueño con su clave).
- Los negocios de test no cambian (su pasarela es sandbox); producción deja de bloquear tickets bajo 150.000.
