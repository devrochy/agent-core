### Arreglado

- Pruebas de integración de los crons de negocio: ya no dependen de la hora en que corre el CI.
  Desde ADR-0201 cada cron de negocio corre solo a su hora local y las pruebas que llaman los
  jobs directo se saltaban «fuera de hora» (17 en rojo en `develop` tras agent-core#823).
