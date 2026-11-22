## Equipo: roles de pack solo de módulos activos y quitar el perfil de gestor (2026-11-21, #870)

- `GET /api/v1/admin/collaborators/pack-roles` ofrece solo los roles de los packs **activos en el negocio**: en su AgentSpec, sin apagar y con un especialista publicado que los cubra. Es la regla con la que el panel habilita cada módulo. Antes bastaba con que el pack estuviera en el spec: `solvershub` tiene `pack_cobranza` en el spec pero no tiene agente de Cobranza publicado, así que el alta ofrecía «Gestor de cobranza».
- El alta de colaboradores y las invitaciones rechazan con 400 (`collaborator_error`) un rol de pack que el negocio no ofrece, antes de escribir nada. Al aceptar una invitación vieja se descartan los roles cuyo pack ya no está activo.
- Nuevo `POST /api/v1/admin/collaborators/{admin_id}/pack-roles/revoke` (`{"value": "pack_cobranza:gestor"}`): quita el rol de pack en el negocio activo y la cuenta vuelve a la política de Colaborador. Vale aunque el pack ya no esté activo. Hook opcional nuevo `on_collaborator_role_removed`: Cobranza da de baja lógica el `Gestor` sin tocar pagos ni carteras, e invalida la caché de acceso. Si vuelve a recibir el rol, se revive el mismo perfil.
- Equipo (`pack_profiles`) muestra los perfiles de todos los packs instalados, no solo de los habilitados, igual que `rol_de_acceso`.
- Sin cambios de esquema.
