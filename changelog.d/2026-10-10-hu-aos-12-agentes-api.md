## Agent OS: agentes versionados por API y tools declarativas sin secretos (2026-10-10)

- HU-AOS-12 (parte 2, #373). **API de agentes** en `/api/v1/admin/agents` (ADR-0113, ADR-0077):
  - crear un agente (`kind` core → `kernelspec/1`; specialist o backoffice → `agentspec/3`)
    con su borrador;
  - guardar el borrador con `lock_version`: 409 si otra persona lo cambió, 422 con los errores
    del contrato por campo (el mismo validador del CLI y de CI);
  - publicar una versión inmutable con `content_hash` (sha256 del JSON canónico), idempotente
    por hash. Publicar no activa nada: las releases llegan con HU-AOS-14.

  Con el runtime `legacy` solo escribe el master (o un negocio con `aos_runtime = 'v3'`). Las
  keys `general` y `coordinador` quedan reservadas para el backfill de HU-AOS-13.
- **Editor de tools declarativas** en `/api/v1/admin/settings/declarative-tools` (antes solo
  SSR). Los headers sensibles van por referencia `{"secret": ...}` y los valores son de solo
  escritura. El GET enmascara los literales viejos; guardarlos con la máscara los migra. El
  SSR pasa por el mismo validador y ya no muestra secretos.
- **Secretos en la instalación `core`** del negocio (`pack_installations.config_encrypted`,
  ADR-0118 §5). Van cifrados con AAD `(tenant_id, installation_id)` y cada secreto está atado
  a los dominios de las tools que lo usan. `core/mcp/declarative.py` los resuelve al ejecutar,
  solo hacia esos dominios; los literales v2 siguen funcionando igual. `migrar_secretos` es el
  paso del backfill antes de la primera `agent_version`.
- Migración `0076`: fila `core` en el catálogo `plugins` (FK de `pack_installations`).
