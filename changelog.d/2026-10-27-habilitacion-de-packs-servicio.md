## La habilitación de packs sale de la capa SSR (2026-10-27)

- Refactor sin cambio de comportamiento: `set_pack_for_tenant`, `configure_plugin_for_tenant` y sus
  auxiliares (activar, cuota, habilitar, deshabilitar, sembrar el flujo) pasan de `core/admin/views.py` a
  `core/agents/habilitacion.py`. `views.py` los reexporta con los mismos nombres, así que
  `/admin/plugins/{id}/action`, `PUT /admin/plugins/{id}/config` y el panel SSR siguen igual.
- Prepara que el editor de agentes habilite las habilidades de una plantilla sin importar de la capa SSR
  (el onboarding deja de exigir la habilidad habilitada de antemano). Sin tablas ni migración.
