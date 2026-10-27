## Salida interactiva tipo lista de WhatsApp (2026-10-27)

- `sender.interactivo_valido` acepta `{"type": "list", "body", "button", "rows": [{"id", "title", "description"?}]}`
  con los topes de Meta: ≤10 filas, id ≤200, título ≤24, descripción ≤72, texto del botón ≤20, cuerpo ≤1024
  e ids de fila únicos. Una sola sección en esta primera versión.
- `WhatsAppMetaOutbound.send_interactive` arma `action.sections[0].rows[]` para listas (descripción vacía no viaja).
- Sin migración: el CHECK `ck_messages_interactive_objeto` (0082) ya admite `'list'`. La respuesta
  (`list_reply`) ya la parsea el entrante como `interactive_id`. Base de HU-INMO-03.
