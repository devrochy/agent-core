## Paquete `channel-twilio` (PR-2 de WhatsApp vía Twilio, ADR-0186) (2026-11-07)

- Nuevo `packages/channel-twilio`, registrado como el proveedor `twilio` en `agent_core.channels`. Trae:
  - la firma `X-Twilio-Signature`, igual que twilio-python y probada con sus vectores, que acepta la URL con puerto y sin él;
  - un cliente HTTP con auth básica, limitado a los hosts de Twilio y con los SID validados antes de armar la URL;
  - la tabla de códigos de Twilio a categoría de `ProviderError`;
  - Senders API v2 (listar, consultar y registrar webhooks) y Content API (crear, enviar a aprobación, consultar y listar plantillas, y validar sus variables);
  - el normalizador del status callback, con el mismo dict que lee `apply_status_update`;
  - los adaptadores de entrada (formulario → `InboundMessage`, con media, botón y lista) y de salida (texto, imagen y plantilla por `ContentSid`).
- Todavía no se usa: el tipo `whatsapp_twilio` sigue «Próximamente» hasta el PR-3 (alta y bandera por negocio). Sin migraciones ni rutas.
