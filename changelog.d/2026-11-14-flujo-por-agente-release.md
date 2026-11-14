## Flujo de atención por agente en la release (2026-11-14)

- El manifiesto congela el flujo de cada agente (`flow` + `flow_hash`) y la release lo compila
  por agente; `flow_tenant` queda para las releases anteriores (ADR-0195, HU-AOS-75).
- `resolve_flow` acepta `flow_agente`: compone core + las secciones del agente, sin volver a
  mezclar los defaults de sus packs.
- Los procedimientos abiertos de un agente se derivan de su flujo; un agente sin flujo propio
  sigue con los del negocio.
- Nota: los gates y veredictos de release siguen leyendo el `flow_tenant` global; se revisará
  cuando termine el retiro del General/legacy.
- La semilla del flujo no se crea sola al publicar: mientras el agente no tenga flujo propio se
  sigue leyendo `flow_tenant` (conducta anterior y evals). El panel la guarda cuando el dueño
  confirma el paso «Flujo de atención».
