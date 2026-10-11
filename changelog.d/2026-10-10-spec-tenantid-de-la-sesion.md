## Aislamiento: el negocio del prompt sale de la sesión, no del `tenantId` del AgentSpec (2026-10-10)

- `PUT /api/v1/admin/settings/advanced/spec` y el editor crudo del panel SSR guardaban el `tenantId`
  que trajera el JSON. Como el prompt resolvía `TENANTS_ROOT/<slug>/` desde ese campo, un negocio podía
  cargar los playbooks y el `flow.json` de OTRO negocio (hallazgo del auditor multi-tenant durante
  HU-AOS-12).
- `save_spec_version` y `save_spec_if_changed` fuerzan `tenantId` al negocio que guarda
  (`con_tenant_de_la_fila`); un JSON que solo cambia el `tenantId` no crea versión. El endpoint devuelve el
  spec con el `tenantId` de la sesión.
- `build_system_blocks`, `load_playbook` y `resolve_flow` sacan el slug de `current_tenant_slug()` (el
  `tenant_scope`), nunca de `spec.tenantId`. Fuera de un scope no hay override por negocio. Las filas ya
  guardadas con un `tenantId` ajeno dejan de cargar artefactos de otro negocio (y el lector tolerante v3
  sigue avisando de la diferencia).
- `evals/runner.py` arma el flujo y el prompt dentro del `tenant_scope` del negocio evaluado, así que sus
  overrides se siguen cargando igual que antes.
