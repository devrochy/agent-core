## Test del sandbox del playground estable (2026-11-14)

- `test_pedido_simulado_no_toca_order_ni_consecutivo` compara cuántos pedidos tenía el
  tenant antes y después del pedido simulado, en vez de exigir que no haya ninguno. El
  tenant de prueba es compartido con otras suites que crean pedidos y, con xdist, el
  total dependía del orden de los tests (falló en develop @ fd7c197).
