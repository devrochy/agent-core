## Los PR a `develop` no pueden romper el contrato de la API sin avisar (2026-11-09)

- **CI compara `docs/openapi.json` del PR contra `develop`** con `oasdiff` y falla si hay un cambio incompatible en `/api/v1`. Antes solo se comprobaba hacia `main`, y la APK instalada no se actualiza con el backend.
- **Un valor nuevo en un enum de respuesta solo avisa:** es la forma habitual de ampliar esos campos y no bloquea el PR.
- **Cambio incompatible intencional:** etiquetar el PR con `api-incompatible` y relanzar el job.
