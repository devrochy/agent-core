## Suscripciones: las tools `sub_*` se ofrecen desde el primer arranque (2026-10-10)

- El predicado de oferta de `sub_consultar`, `sub_link_pago`, `sub_pausar`, `sub_cancelar` y
  `sub_cambiar_plan` lee los proveedores del registro de suscripciones en cada request. Antes los
  calculaba una sola vez, al registrar las skills del core, y ese conjunto quedaba fijo en el proceso.
- En la API (`core/main.py`) las skills del core se registran al importar el módulo y los packs se
  activan después, en el lifespan: el conjunto quedaba vacío y ningún negocio con
  `pack_subscriptions` recibía las `sub_*` en el consumer, el debounce ni el playground, en ningún
  arranque (reiniciar no lo arreglaba). El worker de Celery no corre turnos del asistente, así que no
  lo afectaba.
- `evals/journeys.py` sobre una base recién migrada ya ofrece las `sub_*` desde la primera corrida
  (antes solo desde la segunda, visto en el PR #409).
- Sin cambios en qué tools se ofrecen con todo cargado: siguen saliendo solo si el negocio tiene
  habilitado un proveedor de suscripciones registrado, y dejan de ofrecerse si el pack se desactiva.
