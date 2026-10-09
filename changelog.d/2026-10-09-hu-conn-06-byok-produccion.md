## BYOK de producción con registro de número y promoción (2026-10-09)

- HU-CONN-06 (ADR-0127). El wizard BYOK de la API elige el entorno (`environment`: `sandbox` o `production`) y lo
  guarda en la conexión. Antes BYOK quedaba siempre en prueba. En producción se rechaza el número de prueba de
  Meta (`account_mode = SANDBOX`) y el alta pide ser propietario del negocio o master.
- Registro del número con PIN: si Meta no reporta el número registrado en la Cloud API (`platform_type`), la
  conexión queda `pending` ("Falta registrar el número en la Cloud API con su PIN") y pasa a `connected` al
  registrarlo, con el PIN en el mismo alta (`pin`) o desde "Números". Antes el alta lo marcaba registrado a ciegas.
  En una BYOK de producción el PIN es obligatorio (no se genera uno que nadie ve).
- "Pasar a producción" de una conexión BYOK de prueba: `GET/POST /api/v1/admin/meta/connections/{id}/promotion`
  (`/number`, `/verify`, `/finish`). El número real crea la conexión de producción de la misma app con el alta BYOK
  (la de prueba sigue funcionando); con registro, plantillas aprobadas y los chequeos de la app del negocio en
  verde, la de prueba se da de baja. Sus conversaciones nunca se responden desde el número real.
- Los chequeos de producción de una conexión BYOK usan su app (token, App ID y App Secret de la conexión), nunca
  la de la plataforma, y exigen que la WABA esté suscrita a esa app. `/promotion/verify` también sirve para una
  BYOK creada directo en producción.
- Cada alta BYOK guarda un `verify_token` propio (el que escribe el admin o uno aleatorio) en vez de registrar el
  global, y el wizard rechaza el App ID de la app de la plataforma (seguimientos de ADR-0126).
- `origin` en `GET /api/v1/admin/meta/health`. "Diagnosticar" no mejora una conexión que espera el registro, pero
  sí la pasa a `error` si el token murió. Registrar con PIN ignora los números dados de baja.
