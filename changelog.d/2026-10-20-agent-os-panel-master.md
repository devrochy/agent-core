## Agent OS por negocio desde el panel del master (2026-10-20)

- `GET`/`PUT /api/v1/admin/platform/tenants/{id}/agent-os` y `POST .../agent-os/simular` (HU-AOS-15,
  «Orden de banderas»): solo el master (también en test, no el tester) fija runtime, procedimientos y ruteo; simular es un
  dry-run con bloqueos, avisos y pasos. Al pasar a v3 corre los backfills del negocio y `migrar-a-v3`
  (`construir` opcional). 422 fuera de orden, 409 con los bloqueos.
- Migración 0097: `aos_fijar_banderas` (SECURITY DEFINER), escritor único y auditado de las tres banderas,
  y el trigger `tenants_aos_banderas_guard`, que impide al rol de la aplicación escribirlas directo.
- `migrar-a-v3`, `db ruteo` y «Preparar mis datos» escriben con esa función; «Preparar mis datos» y el panel
  comparten el candado por negocio. Adenda HU-AOS-15 de ADR-0112.
