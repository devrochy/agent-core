## Diagnóstico de Meta que dice la verdad (2026-10-08)

- HU-CONN-04. `GET /api/v1/admin/meta/health` devuelve `checks` (solo los checks, en orden y con su
  `repair_action`) y `billing_manual_status`. `health` guarda también datos del alta (`coexistence`,
  `token_scope_check`, `webhook_subscription_active`) y el panel los listaba como chequeos.
- Marcar la facturación a mano guardaba solo `manual_status`: el check `billing` desaparecía de la lista hasta
  el próximo diagnóstico y su detalle quedaba viejo. Ahora se rearma con el estado nuevo.
- La vista de salud ya no lista conexiones eliminadas.
- Test de integración del "sin verde falso": una conexión `connected` con el token rechazado por Meta pasa a
  `error` con el motivo y el check queda primero con `reauthorize`.
