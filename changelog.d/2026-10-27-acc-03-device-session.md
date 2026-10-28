## Dispositivos de todos los roles: `device-session` y control en el principal (2026-10-27)

### Añadido
- **`POST /api/v1/access/device-session` (HU-ACC-03, ADR-0174):** la APK registra o consulta su equipo tras elegir negocio y al reanudar, con cualquier rol (dueño, colaborador, gestor) y solo para su propia cuenta. Aplica la política del rol: dueño y colaborador `abierto`, gestor `restringido` por defecto. El master de plataforma nunca se registra ni se bloquea.
- **Hook opcional `PackModule.rol_de_acceso`:** el pack de cobranza informa qué cuentas son `gestor`; el core no importa del pack.
- **Control de equipo en toda la API `/api/v1`:** si la petición trae `X-Device-Id` de un equipo bloqueado o pendiente, responde 403 con `code` (`dispositivo_bloqueado`, `dispositivo_pendiente`). Sin el header no cambia nada (el panel web sigue igual). Exentas `device-session`, `/admin/me`, `/auth/*` y `gestor-token`. Caché de 60 s; con Redis caído se lee la base.

### Cambiado
- El token de campo del propietario también lleva `did` y se revalida en cada petición y en el refresh.
- **Cambio de contrato:** un login de campo sin `X-Device-Id` ni `imei` en política restringida responde 403 `dispositivo_requerido` (antes tokens con `requiereVinculacion`). En política abierta entra con el aviso `dispositivo_sin_id`.
