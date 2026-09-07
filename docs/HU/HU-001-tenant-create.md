# HU-001 · Dar de alta un tenant

**Como** admin master, **quiero** dar de alta un cliente nuevo en minutos desde el
CLI o el panel, **para** tener su asistente configurado (AgentSpec, canales, packs)
sin escribir código.

## Criterios de aceptación

- [ ] `agent-core tenant create` pregunta: nombre, slug, negocio, idioma, tono,
      packs, triggers de escalación y WhatsApp (opcional).
- [ ] La misma operación está disponible en el panel: `/admin/tenants` → **+ Nuevo tenant**
      (wizard en 5 pasos, visible solo para admins `master`).
- [ ] Al terminar crea: `tenant`, `agent_specs` (activo, con los packs elegidos y
      los playbooks del pack), `app_settings`, `tenant_user` (vincula al admin) y
      `channel_connections` si se conectó WhatsApp.
- [ ] Idempotente: repetir el onboarding no duplica filas.
- [ ] El tenant nuevo aparece en el selector del panel y su spec es editable en
      `/admin/spec`.
- [ ] Los datos del cliente se importan desde `TENANTS_ROOT/<slug>/catalog.json`
      con `agent-core tenant seed <slug>`; la documentación del tenant vive en
      `TENANTS_ROOT/<slug>/docs/` (fuera del framework).
- [ ] Si algún paso requirió escribir código, es un pack incompleto → anotar deuda
      en `docs/ROADMAP.md`.

## Flujo técnico

`cli/commands/tenant.py` (y `core/admin/views.py` → `/admin/tenants/nuevo`) llaman
al servicio `core/onboarding.py` (`onboard_tenant`), que persiste las filas dentro
de `tenant_scope` (RLS) y habilita los packs (migraciones por tenant).

## Relación con el resto

- `agent-core init` prepara el despliegue (una vez); `tenant create` da de alta
  clientes (las veces que sea). Ver `docs/MULTITENANT.md`.