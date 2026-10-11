## Purga de un negocio: Redis se limpia después del commit (2026-10-10)

- `purge_tenant` limpiaba el estado Redis del negocio ANTES de la función SQL: si la purga
  fallaba y hacía rollback, el negocio seguía existiendo sin su memoria de trabajo, debounce ni
  marcadores. Ahora Redis se limpia solo después del commit y de verificar que la fila `tenants`
  ya no existe; una purga SQL fallida deja Redis intacto.
- Si la limpieza de Redis falla tras el commit, la purga no se revierte: se registra
  `tenant_purge_redis_cleanup_failed` (error, con `tenant_id` y comando de reintento, sin slug),
  se manda una alerta `critical` y `purge_tenant` devuelve `False` (antes `None`). La CLI sale
  con código 2 y el panel SSR muestra un aviso; la API sigue respondiendo 204.
- Reintento idempotente: `agent-core tenant purge-redis <tenant_id>` (`purge_tenant_redis`).
  Solo acepta el UUID y se niega si el negocio todavía existe o si el UUID aparece en Redis como
  id de un hijo de otro negocio.
- La limpieza estaba desfasada de los formatos de clave reales: borraba `wm:{conv}` (hoy la
  memoria es `wm:{tenant}:{conv}`) e `inbound:meta:{id}` (hoy `inbound:{tenant}:...`), y no tocaba
  `debounce:messages`, `alta:terminos`, `waweb:*` ni el resto. Ahora borra las claves por
  conversación y hace `SCAN MATCH *<tenant_id>*` + `UNLINK` de lo que lleva el UUID en posición
  de tenant (primer UUID del nombre). La convención de claves queda en `docs/MULTITENANT.md`.
