## Webhook y secreto por conexión (2026-10-09)

- HU-CONN-05 (ADR-0125). Un negocio con varias conexiones de WhatsApp (una BYOK con su app y otra de
  Embedded Signup, o dos apps propias) recibe de todas: cada mensaje se verifica con el App Secret de la
  conexión dueña de su número o WABA (`core/gateway/webhook_secrets.py`). Antes había un solo secreto por
  negocio y la otra conexión daba 401.
- El secreto lo elige la base, no el cuerpo: el de una conexión no autoriza los números de otra, ni del mismo
  negocio (`signature_connection_mismatch`). Por el slug, un número que no es de ninguna conexión del negocio
  ya no se le atribuye: 401 o `unresolved_tenant`.
- El handshake `GET /webhooks/whatsapp/<slug>` acepta el `verify_token` de cada conexión del negocio.
- "Registrar webhook" por conexión: `POST /api/v1/admin/meta/connections/{id}/webhook/register`. En una BYOK
  registra la app de esa conexión con la URL del negocio y su token, sin caer nunca a la app de la plataforma;
  en una de Embedded Signup (app de la plataforma) solo lo puede hacer el master. `POST /webhooks/register`
  queda obsoleta y `GET /webhooks` trae `connection_id`, `origin`, `uses_platform_app` y `can_register`.
