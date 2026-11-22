## Marca propia, historial por plan y exportación de conversaciones (2026-11-22)

- HU-BILL-25 (SPEC-51, D-79). Tres features del catálogo v3 que no gastan créditos, en `core/billing/features_del_plan.py`. Un negocio sin suscripción o con un plan desconocido no se limita, igual que en `canales_del_plan`. La excepción es la marca, que se sigue mostrando.
- **Catálogo:** feature nueva `exportar` en los cuatro planes. Free y Basic `false`; Premium y Empresarial `true`.
- **Marca propia:** `GET /api/v1/public/web-chat/{key}/config` trae `marca_propia`, calculada del plan: `true` en Premium y Empresarial.
  - Si falla el billing, sale `false` y el widget no se cae.
  - El texto «SolversHub» vive en la landing (`app/w/[key]/page.tsx`, `assistantName="SolversHub"`). Allí falta leer el campo nuevo.
- **Historial: solo se oculta en el panel** (decisión del dueño 2026-11-22, reemplaza el «cron de retención que archiva» de la HU). No se borra, no se archiva y no hay cron.
  - Una conversación se ve si empezó o tuvo un mensaje en los últimos `historial_dias` del plan vigente. De ella solo salen los mensajes de esa ventana. El corte se calcula en cada request: al subir de plan se ve todo al instante.
  - Filtran el historial: `GET /admin/conversations`, `GET /admin/conversations/{id}`, `GET /admin/conversations/{id}/messages`, el buscador (`GET /search`, sección conversaciones), las recientes de `GET /admin/dashboard` y `GET /admin/agents/{ref}/conversations` (el período no empieza antes del corte).
  - **Conversación fuera de la ventana pedida por id:** 409 `plan_feature_not_included` con `details = {feature: "historial", plan, historial_dias}`, y un mensaje que dice que no se borró nada. Se elige el aviso sobre el 404 porque la ficha del cliente, las escalaciones y las notificaciones enlazan ids de conversación.
  - No cambian: el turno y su memoria (`_load_history`, Redis, recall), los crons, las rutas master de plataforma, las escalaciones, el tablero de fiabilidad y las acciones sobre una conversación (responder, tomar, devolver, resolver, notas).
- **Exportar:** `GET /api/v1/admin/conversations/export.csv` (sección `conversations`).
  - CSV UTF-8 con BOM, una fila por mensaje. Columnas: Conversación, Cliente, Identificador, Canal, Conexión, Estado de la conversación, Inicio, Cierre, Mensaje, Fecha del mensaje, Dirección, Remitente, Estado del mensaje, Contenido, Tipo de archivo, Transcripción.
  - Respeta el historial, excluye el Centro de pruebas, no trae URLs de archivos y neutraliza fórmulas.
  - Se transmite por lotes de 500 con un cursor del servidor, dentro de su propio `tenant_scope`, y filtra por `tenant_id`. Tope de 100 000 mensajes; si se recorta, `X-Export-Truncated: true`.
  - En Free y Basic responde 409 `plan_feature_not_included` con `details = {feature: "exportar", plan}`.
- **API (`docs/openapi.json`):** ruta nueva de exportación y 409 documentado en el detalle y los mensajes de una conversación. La config pública del widget no tiene `response_model`, así que `marca_propia` no aparece en el OpenAPI.
- `celda` (neutraliza fórmulas en CSV) pasa de `leads.py` a `core/shared/csv_seguro.py`. La usan clientes y conversaciones.
