## Tests de crons sin depender de la hora del CI (2026-11-17)

- Los tests de integración de los jobs (suscripciones, cobranza y demás crons de negocio) ya no
  dependen de la hora del reloj: desde ADR-0201 `debe_correr` deja pasar solo la hora local por
  defecto del negocio (22/21/18 en Bogotá) y la lee del reloj real, no del `now` que el test le
  pasa al job. Una fixture autouse de `tests/integration/conftest.py` deja pasar la política en
  esos tests (antes solo la tenía `test_subscription_descuento_n_periodos.py`); la política se
  sigue probando con `svc.politica` en `test_crons_negocio_api.py`. Sin cambios en producción.
