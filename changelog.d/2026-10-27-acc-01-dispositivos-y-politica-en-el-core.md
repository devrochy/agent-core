## Dispositivos de la cuenta y política de acceso en el core (2026-10-27)

- HU-ACC-01 (SPEC-42, ADR-0174). Migración `0113`: tablas `account_devices` (un equipo por cuenta y negocio, baja lógica) y `access_events` (append-only) con RLS forzada, y copia de las concesiones `pack_cobranza:dispositivos` → `devices` y `pack_cobranza:alertas` → `access_alerts` (idempotente, reversible).
- `core/access/`: `evaluar` (regla de decisión: un equipo bloqueado deniega siempre, el alta es `INSERT … ON CONFLICT`), transiciones con estado esperado (409 si cambió) y política por negocio y rol en `tenants.settings.acceso.dispositivos` (default: owner y colaborador abiertos, gestor restringido).
- API `/api/v1/access`: `GET|POST /devices`, `GET /devices/{id}`, `POST /devices/{id}/aprobar|bloquear|dar-de-baja`, `GET /events` (keyset) y `GET|PUT /policy`. Secciones nuevas del RBAC `devices` y `access_alerts`, opt-in para colaboradores; los equipos del dueño solo los administra un dueño o el master.
- Sin cambio de comportamiento en el login ni en las rutas de cobranza: el enforcement llega con HU-ACC-02/03.
