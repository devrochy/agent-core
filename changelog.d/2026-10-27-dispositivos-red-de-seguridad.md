## Dispositivos del gestor: un equipo bloqueado ya no abre el acceso (2026-10-27)

- Cobranza. `_enforcement_dispositivo` deniega (403) un equipo en estado `bloqueado` aunque sea el único dispositivo del gestor; antes `activos_imei` quedaba vacío y el bloqueo equivalía a quitar la restricción. El intento queda en «Alertas de acceso» con el motivo `dispositivo bloqueado`.
- Cobranza. `POST /gestor/dispositivo` busca el equipo por `(tenant_id, imei)`, como el índice único: si ya es de otro gestor del negocio responde 409 (antes: `IntegrityError` → 500).
- Pruebas. Nuevo `tests/integration/test_pack_cobranza_dispositivos_enforcement.py` (403 por dispositivo no autorizado, bloqueado, vinculación idempotente, `/devices`, `/devices/{id}/status`, `/alerts` y aislamiento entre negocios). Red de seguridad previa a mover «Dispositivos» al core.
