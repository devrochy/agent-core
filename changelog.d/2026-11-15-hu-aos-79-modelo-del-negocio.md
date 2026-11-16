## El agente usa el modelo del negocio cuando el suyo no sirve (2026-11-15)

- HU-AOS-79 (SPEC-47, D-75). Publicar un agente con un modelo retirado o de otro proveedor ya no
  responde 422 `modelo_retirado` / `modelo_de_otro_proveedor`: la versión se congela con el
  modelo por defecto del negocio (`modelo_del_negocio`: el de «Cuenta y proveedor», si no el del
  despliegue cuando el proveedor lo sirve, si no el primero vigente de ese proveedor) y lo anota
  en sus notas y en `warnings`. El borrador no cambia; un modelo válido se respeta.
- `GET /api/v1/admin/agent-models` suma `business_model` y, con `?agent_id=`, `effective_model` y
  `effective_source` (`negocio` o `agente`); 404 si el agente es de otro negocio. Campos aditivos.
- `versiones.publicar(..., modelo=)` reemplaza `model.primary` solo en la versión.
