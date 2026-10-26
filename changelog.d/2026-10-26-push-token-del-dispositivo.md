## El token de push se reasigna al cambiar de cuenta en el teléfono (ADR-0168)

`POST /api/v1/me/push-token` ya no responde `409 token_de_otra_cuenta` cuando el token lo tenía otra cuenta del negocio: el token identifica al dispositivo, así que pasa a la cuenta que lo registra ahora (y la anterior deja de recibir). Antes, cambiar de cuenta en el mismo teléfono lo dejaba sin push. Se registra `push_token_reasignado`. Enmienda ADR-0099.
