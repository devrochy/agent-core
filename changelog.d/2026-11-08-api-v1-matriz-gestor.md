## La API v1 de campo aplica la matriz de permisos y las carteras del gestor (2026-11-08)

- agent-core#690. `POST /sync/eventos`, `GET /carteras/{id}/clientes-dia` y `POST /clientes/{id}/evidencia` aceptan el mismo token de cuenta que la API v2 y no aplicaban ni la matriz ni las carteras: un gestor sin `registrar_pago` registraba el pago por aquí, en cualquier cartera del negocio. Ahora aplican lo mismo que la v2, y `/sync/eventos` devuelve también `rechazados` (los eventos sin permiso no se guardan y se pueden reintentar). El propietario, el master y los dispositivos sin gestor no cambian.
