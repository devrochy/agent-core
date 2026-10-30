## Eventos de acceso ampliados y límites de equipos (2026-10-30)

- ADR-0174, HU-ACC-06. `login_fallido` y `login_bloqueado` quedan en cada negocio de la cuenta (tope 10) desde `POST /api/v1/auth/login`, el login unificado y el login por negocio de la app de campo, y el login v1 (código + API key, canal `campo_v1`). Un identificador que no es cuenta no escribe filas; el detalle no lleva el identificador. Dedupe de 5 min por negocio, cuenta, equipo, tipo y canal (las repeticiones se anotan en el siguiente evento).
- El login unificado de campo (`POST /api/pack/pack_cobranza/auth/login`) ahora tiene bloqueo progresivo (5 fallos en 10 min → 15 min, 429).
- Límites de `device-session`: tope de 10 equipos vigentes por cuenta (429 `demasiados_equipos`) y 30 consultas por minuto por cuenta (429 `demasiadas_solicitudes`).
- Asignar el rol `gestor` a un colaborador baja a `pendiente` sus equipos que entraron por política abierta (los aprobados por una persona se respetan); cambiar la política (`PUT /access/policy`) invalida la caché de vigencia de todo el negocio; el tester habilitado se trata como master en `device-session`.
- Tarea diaria `core.access_events_retention`: baja lógica de los eventos de más de 180 días. Sin migración.
