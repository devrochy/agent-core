## Datos de prueba por módulo

- `GET/POST/DELETE /api/v1/admin/datos-de-prueba/{modulo}` generan y retiran el lote de Ventas,
  Agenda, Cobranza o Suscripciones sin depender de la ficha de un agente (sirve también para el
  onboarding).
- Suscripciones crea planes en Comercio y, con su snapshot, clientes, suscripciones y cargos en
  el pack de Suscripciones.
- El retiro por módulo alcanza también los lotes generados desde los agentes.
- Comercio ahora hace borrado lógico de productos: antes el botón desactivaba variantes y
  borraba categorías, pero los productos seguían apareciendo en el panel.
