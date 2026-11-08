## ADR-0186 y ADR-0187: WhatsApp vía Twilio (2026-11-07)

- Se proponen dos ADR, sin cambios de código.
- **ADR-0186:** conexión WhatsApp vía Twilio con la cuenta del negocio (BYOK). Cubre:
  - el alta guiada, con descubrimiento del número y registro automático de webhooks;
  - el webhook por conexión, firmado contra la URL registrada;
  - las plantillas de la Content API, con su polling de aprobación;
  - botones y listas;
  - los errores por categoría y la salud.
- **ADR-0187:** fase ISV con una subcuenta por WABA. Está bloqueada hasta que Meta verifique a solvershub.
- Producto: D-63, HU-CONN-11 a HU-CONN-18 (docs).
