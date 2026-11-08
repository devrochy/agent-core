## Observabilidad B2: autoinstrumentación de FastAPI, SQLAlchemy y httpx (ADR-0188) (2026-11-08)

- Con `OTEL_ENABLED=true`, spans por petición HTTP (sin `/healthz*` ni los spans internos de send/receive), por sentencia SQL del engine del core y por llamada httpx de cualquier cliente del proceso.
- Redacción: la query sale de `http.url`/`url.full` y `url.query` se tapa, tanto en el servidor (el GET del webhook trae `hub.verify_token`) como en httpx (tokens de Meta, `client_secret`). `db.statement` es el SQL del cursor con marcadores (`$1`), sin valores; sin sqlcommenter.
- Las tres instrumentaciones usan un `MeterProvider` no-op: sus histogramas llevan ruta, host y código, y no caben en el presupuesto de series.
- No se instrumentan Celery (ya lo propaga `celery_trazas.py`) ni Redis.
- Dependencias nuevas: `opentelemetry-instrumentation-fastapi`, `-sqlalchemy` y `-httpx` (`>=0.66b1`, la serie del SDK). SQLAlchemy se instrumenta con `skip_dep_check`: la 0.66 declara `sqlalchemy < 2.1` y la imagen instala 2.1.
- Errores sin datos en los spans: `ExportadorRedactado` deja solo el código del status y el tipo de la excepción (el instrumentador de SQLAlchemy ponía `str(exc)`, con el `DETAIL` de asyncpg; FastAPI registraba el mensaje). El token de `GET /api/v1/files/{token}` se tapa en `http.target`/`url.path`/`http.url`. El engine del core usa `hide_parameters=True`: los errores de SQL ya no llevan `[parameters: …]` en logs, spans ni Sentry.
