### Cambiado
- **El token de campo del gestor lleva el equipo (`did`) y se revalida en cada petición y en el refresh (HU-ACC-02, ADR-0174).** Bloquear (o dar de baja) el equipo corta la sesión abierta con 403 `{error, code}` (`dispositivo_bloqueado`, `dispositivo_pendiente`, `dispositivo_no_autorizado`) en menos de 60 s: la vigencia se cachea en Redis 60 s y se invalida al escribir; si Redis no responde se lee la base (nunca se abre el acceso).
- `POST /auth/refresh` vuelve a decidir el equipo: con `X-Device-Id` sobre ese equipo; sin él, con el `did` del token. Un bloqueo corta también la renovación.
- Un token de gestor sin `did` (app vieja con el equipo por vincular) solo alcanza `GET/POST /gestor/dispositivo` mientras la política del gestor sea `restringido`; con política `abierta` pasa como antes.
- **Transición:** los tokens emitidos antes de este cambio no traen `did`. Mientras la cuenta tenga un equipo activo con id, siguen valiendo y el refresh les asigna ese equipo: no se desloguea a nadie al desplegar.
- Un `AccesoError` sin capturar fuera de `/api/v1` responde 403/409 con `{error, code}` en vez de 500.
