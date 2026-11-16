## Ventas respeta los `writeNamespaces` del agente al escribir el perfil (ADR-0114 §3, ADR-0179 §10) (2026-11-16)

- `crear_lead`, `actualizar_lead` y `crear_pedido` (pack_commerce) escriben el perfil del cliente por `datos_cliente.guardar()` con `concedidos=context.write_namespaces`. Lo encontró el auditor multitenant sobre agent-core#816. Era dentro del mismo negocio, sin fuga entre negocios.
  - Antes, los dos de leads no pasaban los `writeNamespaces` y `crear_pedido` escribía directo, sin pasar por la concesión ni por el aviso de datos.
  - El lead o el pedido se guarda siempre. Si el agente no tiene `perfil` concedido o el dato no se admite, solo se omite la copia al perfil (logs `ventas_perfil_no_concedido` / `ventas_perfil_dato_no_admitido`) y la tool responde igual.
  - `crear_pedido` sigue guardando solo con `guardar_datos_cliente` (D17). Con el aviso de datos activo y sin aceptación, guarda solo los campos requeridos de la acción (`ventas.pedido_domicilio`/`ventas.pedido_retiro`) y el resto espera. Con el aviso apagado (lo predeterminado), el comportamiento no cambia. La copia al perfil ahora se hace después de crear el pedido: si el pedido falla, el perfil no cambia.
  - Los valores vacíos ya no pisan datos del perfil.
- `datos_cliente.guardar()` exige `concedidos` (keyword-only, sin valor por defecto), así que una tool nueva no puede olvidarlo.
- El test estático de ADR-0179 ya no exceptúa `pack_commerce/skills.py`: esa excepción explica por qué no detectó la escritura directa.
- Se corrigió el docstring de `core/memory/facts.py`: las tools de los packs ya no figuran como escritoras sin chequeo.
- No hay migración ni cambio de API.
