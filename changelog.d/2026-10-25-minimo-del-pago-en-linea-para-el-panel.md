## Mínimo del pago en línea para el panel (HU-SUB-25)

El panel debe avisar, al guardar un plan, que un cobro bajo el mínimo no podrá tener link de pago, y ese mínimo depende de la pasarela y del ambiente (150.000 en sandbox, 1.500 en producción): el panel no puede llevar la cifra.

- `GET /api/v1/pack/commerce/pago-en-linea/minimo`: devuelve `minimo` (el que el negocio fijó en `pago_en_linea_monto_minimo`, o el que declara la pasarela; `null` si no hay ninguno), su `fuente` (`negocio`, `pasarela` o `ninguna`) y cuántas cuentas de transferencia tiene configuradas.
- `crear_pedido` no cambia: conserva su política de 150.000 cuando ninguna pasarela declara un mínimo.
