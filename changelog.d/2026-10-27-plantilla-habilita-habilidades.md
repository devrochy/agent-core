## Elegir el tipo de negocio habilita sus habilidades (2026-10-27)

- El onboarding ya no exige una habilidad habilitada de antemano. `GET /admin/agent-templates` trae
  `will_enable` (las habilidades que crear el agente habilitará) y `available` solo es falso si el despliegue
  no trae la habilidad (`missing_skills`); antes un negocio nuevo en Free (`spec.packs == []`) veía las 4
  plantillas bloqueadas y no podía elegir el tipo de negocio.
- `POST /admin/agents/from-script` y `/admin/agent-templates/{key}/agents` habilitan las habilidades de la
  plantilla (`core/agents/habilitacion.asegurar_habilidades`) en UNA versión de spec, sin validar `min_plan`
  (el límite comercial es el tope de agentes del plan) y devuelven `enabled_skills`. Una habilidad que no
  activa por falta de configuración queda habilitada y se avisa en `warnings`/`requirements`. Con habilidades
  por habilitar se exige además `plugins:write` (403 `skills_forbidden`); si otra edición del negocio gana la
  carrera, 409 `concurrent_edit` (reintentable). Auditoría `skills_enabled_by_template`.
- El paso `activar_preset` del checklist no aparece si las habilidades del preset ya están habilitadas.
- Contrato aditivo (`will_enable`, `enabled_skills`); sin tablas ni migración.
