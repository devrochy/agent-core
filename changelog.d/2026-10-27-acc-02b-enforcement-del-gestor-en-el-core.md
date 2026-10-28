## El login de campo del gestor decide el equipo en el core (2026-10-27)

### Cambiado
- **El login de campo del gestor decide el equipo en el core (HU-ACC-02, ADR-0174).** `_enforcement_dispositivo`, `POST/GET /gestor/dispositivo` y los wrappers del panel (`GET /pack/cobranza/devices`, `POST /pack/cobranza/devices/{id}/status`, `GET /pack/cobranza/alerts`) usan `account_devices` y `access_events` en vez de las tablas del pack. Un equipo `bloqueado` deniega siempre; la política `abierto/restringido` es por negocio.
- Con el header `X-Device-Id` (app nueva) el 403 lleva `code` (`dispositivo_pendiente`, `dispositivo_bloqueado`). Sin él (app vieja, solo `imei` en el cuerpo) se conserva el contrato anterior: tokens y `requiereVinculacion`.
- **Equipo compartido entre dos gestores:** deja de ser 409; la segunda cuenta queda `pendiente` y se registra el evento `dispositivo_compartido` (el vínculo es por cuenta).
- Los wrappers del panel llevan `Deprecation` y `Sunset` y aceptan el permiso viejo (`pack_cobranza:dispositivos|alertas`) o el nuevo (`devices|access_alerts`).

### Añadido
- `adoptar_legado`: red de seguridad si `pack_cobranza_0018` no corrió; copia al core, de a un gestor, los dispositivos del pack que falten.
- La fila heredada sin equipo (vínculo manual sin `imei`) se completa con el primer equipo con el que el gestor entra.

### Pendiente (siguientes entregas)
- Claim `did` en el token, revalidación en cada petición y en el refresh (HU-ACC-02, parte c).
- Los intentos del login v1 (código + API key) siguen en la tabla del pack y no aparecen en las alertas hasta HU-ACC-06.
