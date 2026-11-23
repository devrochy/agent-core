## El pack declara su tipo de agente y cada tool su efecto (HU-AOS-92, ADR-0207) (2026-11-23)

- **Tipo de agente en el pack.** Ventas, Agenda, Cobranza y Suscripciones se declaran en
  `packages/pack-<x>/pack_<x>/tipo_de_agente.json` (`AgentTypeManifest`, `BaseModule.tipo_de_agente`). Se retiran
  `core/presets/plantillas/*.json`; `load_plantilla` lee del pack con la misma forma de antes (`PlantillaAgente`),
  así que el editor, el alta, los presets y QA no cambian. El tipo entra al manifiesto `pack/2`
  (`provides.agent_templates`).
- **Efecto de cada acción.** `MCPTool.side_effect` (`read` / `write` / `irreversible`) declarado a mano en las 62 tools
  de la plataforma: 12 irreversibles (mueven dinero, envían algo fuera o cancelan), 24 de escritura y 26 de lectura.
  Va al manifiesto `pack/2` de cada tool.
- **Cambio de conducta:** al cambiar la release, una tarea que solo leyó o escribió algo reversible se **cancela** en
  vez de escalarse (`tuvo_efecto_irreversible`); con una acción irreversible o desconocida se sigue escalando.
  `sub_link_pago`, `confirmar_pago_pedido` y `sub_inscribir` quedan como `write`, para que los 7 procedimientos
  publicados sigan pasando el linter sin subir su versión (las releases ya publicadas se vuelven a lintear).
- **Catálogo Habilidad → Tools.** `catalogo_de_habilidades()` arma `SkillCatalogItem` con sus `ToolCatalogItem`
  (`ActionInfo` pasa a ser el mismo tipo, con `group`, `description` y `requires_confirmation_default`): las del
  core (`core:datos_cliente`, `core:operacion`, `core:leads`, `core:recepcion`, textos en
  `core/agents/habilidades_del_core.json`) y las de cada pack. Las `sub_*` cuentan como de `pack_subscriptions`.
- **Validación al arrancar.** `validar_catalogo()` frena el arranque de la API si una tool de la plataforma no declara
  efecto o grupo, si dos habilidades declaran la misma tool, si un tipo nombra una habilidad inexistente o si un
  procedimiento por defecto usa acciones fuera de las habilidades del tipo. Los plugins de un negocio no cuentan.
  Contrato en `tests/test_manifiesto_agentes_y_habilidades.py`.
- Grupo nuevo: `comprobar_comprobante` → `pagos`, `buscar_conocimiento` → `conocimiento`.
- Suben: pack_calendar 2.7.0, pack_cobranza 0.9.0, pack_commerce 4.6.0, pack_knowledge 0.4.0 y
  pack_subscriptions 0.9.0 (lock regenerado).
