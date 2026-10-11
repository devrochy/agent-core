## Retención del stream de entrada y su DLQ en Redis (2026-10-10)

- El consumer borra (`XDEL`) cada mensaje de `agent_core:inbound` tras ackearlo, también al mandarlo a
  la DLQ. Antes el stream crecía sin límite y guardaba para siempre el texto y el teléfono de los
  clientes, incluidos los de negocios ya purgados (ADR-0053). Un `XDEL` fallido no cuenta como fallo
  del turno.
- Recorte periódico (cada 10 min) con `XTRIM MINID`: del stream, lo ackeado más viejo que
  `INBOUND_STREAM_RETENTION_HOURS` (24 h por defecto), con un corte que nunca pasa del pendiente más
  viejo del PEL ni del último entregado de cada grupo, así que lo pendiente o no leído no se pierde;
  de la DLQ, lo más viejo que `INBOUND_DLQ_RETENTION_HOURS` (168 h por defecto).
- La limpieza Redis de la purga de un negocio (`tenant purge` y `tenant purge-redis`) borra también
  sus entradas de la DLQ (por el campo `tenant_id`, sin tocar las de otros negocios).
- Documentado en `docs/MULTITENANT.md` (estado Redis en la purga) y las variables nuevas en
  `docs/despliegue-coolify.md`.
