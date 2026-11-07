## Aceptar promociones lo registra solo el propietario, y una baja del cliente no se deshace (2026-11-07)

- D-SPEC-33-9, opción C (decisión del dueño del 2026-11-07). `POST /admin/leads/{id}/consentimiento` con `estado: "otorgado"` ahora responde 403 a un colaborador con Clientes en escritura, antes de leer nada; propietario y admin de plataforma siguen igual. `revocado` no cambia (sigue abierto a quien tiene Clientes en escritura).
- Si la última fila del cliente es `palabra_de_baja` (el cliente se dio de baja) o `meta_131050` (WhatsApp ya no entrega marketing), `otorgado` responde 409 `baja_del_cliente` para todos, incluidos propietario y master; la baja hecha por el equipo (`ficha`) se sigue pudiendo deshacer. La lectura de la última fila va con el cliente bloqueado para no perder una baja simultánea.
- ADR-0150 §6 enmendado. Sin migración. `docs/openapi.json`: solo cambia la descripción de la ruta, no los schemas.
- Pendientes fuera de este PR: panel, APK, ayuda y casos nuevos del catálogo de pruebas.
