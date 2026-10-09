## Primeros pasos calculados y embudo de activación (2026-10-09)

- HU-SH-06 (ADR-0124). `GET /api/v1/admin/onboarding` (sección `dashboard`) devuelve los primeros pasos del
  negocio **calculados del estado real**: datos del negocio, WhatsApp conectado y verificado (salud de
  HU-CONN-04), catálogo o servicios, probar el asistente y equipo invitado; además, las habilidades que el
  preset dejó pendientes de configurar (ADR-0110) y el tipo de negocio que espera un plan más alto. Cada paso
  trae un motivo (clave estable) y `puede_actuar` según la escritura del usuario en su sección.
- Hook opcional `BaseModule.contenido_cargado(tenant_id)` (contrato `core/primeros_pasos/contrato.py`): cada
  habilidad dice cuánto contenido cargó el negocio. Lo implementan `pack_commerce` (productos a la venta),
  `pack_calendar` (tipos de cita) y `pack_cobranza` (clientes). El core no lee tablas de los packs.
- Embudo de activación: `onboarding_hitos` (migración `0062`, RLS forzada) guarda la primera vez que se cumplió
  cada paso, con los segundos desde el alta (`tenants.created_at`), más la invitación aceptada y el primer
  mensaje de un cliente real. Idempotente (`ON CONFLICT DO NOTHING`), auditado (`primer_paso_completado`) y en
  el log (`onboarding_hito`). `agent-core tenant embudo [--dias N] [--solo-automaticas]` barre y agrega:
  negocios por paso, porcentaje, mediana desde el alta y cuántos dentro de las 24 horas.
- El canje del enlace de la invitación (`POST /api/v1/auth/accept-invitation`) **abre la sesión**: responde
  200 con `token`, `admin_id`, `tenant_id` y `expires_in` (antes, 204). Enmienda ADR-0121 § 3.8.
- `GET /api/v1/admin/me` suma `invitaciones_pendientes`; `contar_pendientes` usa los mismos filtros que la
  lista (vigentes y de negocios activos).
