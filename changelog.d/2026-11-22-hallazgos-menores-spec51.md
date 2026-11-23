## Hallazgos menores de la revisión de seguridad de SPEC-51 (2026-11-22)

- Los candados por plan (`features_del_plan`, `canales_del_plan`, `campanas_del_plan`, `habilidades_del_plan`) fallan si reciben la sesión de quien llama con otro negocio en el `tenant_scope` (`exigir_tenant_scope`). Antes, la RLS devolvía «sin suscripción» y el candado se abría. Sin sesión no cambia nada.
- `set_active`, `set_primary` y `delete_connection` buscan la conexión con `get_owned`, que filtra el `tenant_id` del scope además de la RLS.
- La exportación de conversaciones (`GET /api/v1/admin/conversations/export.csv`) deja una fila en `audit_log` (`conversations.export`) con quién exportó, cuántos mensajes y desde cuándo.
- `celda()` también neutraliza las celdas que empiezan por un salto de línea.
