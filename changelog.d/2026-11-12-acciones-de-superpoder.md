## Acciones concedidas por un superpoder (2026-11-12)

- `GET /api/v1/admin/agents/{id}/actions` suma `by_superpower`: la acción la concede un superpoder que el agente declara (D-67), así que corre aunque no tenga binding. Campo nuevo, compatible hacia atrás. Sin esto el panel y la APK mostraban «Apagada» acciones que el agente sí usa (p. ej. `crear_pedido` con «tomar pedido»).
