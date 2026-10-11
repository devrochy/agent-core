## Alta automática: un negocio purgado y vuelto a crear no hereda su Redis (2026-10-10)

- El alta automática (ADR-0121) da al negocio un id determinista (UUIDv5 de vendedor + referencia). Si ese
  negocio se purgaba, la limpieza de Redis fallaba y luego se reintentaba o aprobaba el alta, el negocio
  nuevo nacía con el mismo UUID y heredaba las claves sin TTL del anterior (memoria, pausas, marcadores de
  términos). Hallazgo de la revisión del PR #424.
- Ahora, justo antes de crear el negocio y solo si todavía no existe, el alta borra el estado Redis de ese
  id con `limpiar_redis_antes_del_alta` (`core/tenants.py`), la misma limpieza que `purge_tenant_redis`.
  Las claves de otros negocios no se tocan. Si Redis no responde o tarda más de 30 s, el negocio no se crea:
  el alta se reintenta y al tercer intento queda en revisión. El id sigue siendo determinista, así que la
  idempotencia del alta no cambia. No cubre la DLQ de entrada, la sesión de WhatsApp Web en disco ni
  `TENANTS_ROOT/<slug>/`, que la purga tampoco borra (quedan para otro PR).
- Adenda en ADR-0121; `docs/MULTITENANT.md` y el runbook `altas-en-revision.md` lo explican. Tests de
  integración: reintento después de una purga con Redis caído, alta sin Redis y Redis colgado.
