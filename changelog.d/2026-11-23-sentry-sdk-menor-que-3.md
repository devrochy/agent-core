## `sentry-sdk` fijado por debajo de 3 (2026-11-23)

- `sentry-sdk` 3.0.0 (publicado el 2026-11-23) quitó `sentry_sdk.scrubber`, que usa `core/observability/sentry_setup.py`. Como la imagen instala con `pip` sin el lock, la 3.0 entraba en el CI y en los despliegues. Queda `sentry-sdk>=2.69.2,<3` hasta migrar a la API nueva.
