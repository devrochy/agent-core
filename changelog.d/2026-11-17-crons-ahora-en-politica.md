## El cron y su política miran el mismo reloj (2026-11-17)

- Los jobs de suscripciones (`expirar_inscripciones`, `generar_cargos`, `marcar_vencidas`,
  `avisos_de_cobro`) y el de recordatorios de citas le pasan su `now` a `debe_correr(..., ahora=now)`:
  antes la política de ADR-0201 leía el reloj real y el job el suyo. En producción no cambia nada
  (los dos son «ahora»). La fixture de integración ignora «fuera de hora» siempre (apagado y ventana
  siguen contando) y la regla de la hora queda probada en `tests/test_crons_negocio.py`.
