# ADR-0031 — Messenger: `conversations.fb_page_id` y ventana de servicio

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

Se agregó la conexión de Facebook Messenger (Páginas): OAuth vía Facebook
Login, descubrimiento y auto-conexión de todas las Páginas manejables bajo
una única `MetaConnection` (una por tenant+canal, mismo patrón que
WhatsApp), suscripción de webhook por Página, y el primer `OutboundAdapter`
real de Messenger (antes solo de entrada).

`auditor-multitenant` encontró un bug real de severidad media (no una fuga
entre tenants, sino un enrutamiento incorrecto dentro del mismo tenant):
`connect_messenger` agrupa TODAS las Páginas elegibles de un tenant bajo
una sola `MetaConnection` (mismo criterio que WhatsApp, donde una conexión
= un número). `sender.py::_resolve_messenger_credentials` resolvía la
Página tomando la PRIMERA `FbPage` de esa conexión (`.scalars().first()`),
sin saber a cuál de las Páginas pertenecía la conversación puntual. Un
tenant con dos o más Páginas conectadas podía terminar respondiendo "como"
la Página equivocada — un incidente de cara al cliente final, no una fuga
de datos entre tenants.

## Decisión

Se agrega `conversations.fb_page_id` (FK nullable a `fb_pages.id`),
poblada por `core/orchestrator/consumer.py` en cada inbound de Messenger
(resuelve la `FbPage` por `page_id == connection_external_id` — el Page ID
que ya trae el payload del webhook — filtrando `tenant_id` explícito).
`sender.py::_resolve_messenger_credentials` usa `conversation.fb_page_id`
para traer la Página EXACTA; solo cae al criterio viejo ("la primera de la
conexión") para conversaciones anteriores a esta corrección que todavía no
tienen `fb_page_id` poblado.

También se agrega `conversations.service_window_expires_at` (ventana de
servicio de 24h, refrescada en cada inbound) y `fb_pages.
conversation_routing_enabled` (toggle manual del admin, ya que Conversation
Routing se activa en Meta Business Suite, no es automatizable) — ver
`core/gateway/meta_messenger.py`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Una `MetaConnection` por Página en vez de una por tenant+canal | Cambiaría el patrón ya establecido (WhatsApp: una conexión = un número, pero agrupa varios números vía `PhoneNumber`) y el modo de reautorización de `start_or_resume_wizard`, que busca por `(tenant_id, channel, environment)`. Agregar el discriminador a nivel de `Conversation` es un cambio mucho más chico y contenido. |
| Resolver la Página por `Customer.external_id` (parsear el PSID) en vez de una columna nueva | El PSID no identifica la Página, solo al usuario final -- un mismo PSID puede escribirle a más de una Página del mismo tenant sin que haya forma de distinguir cuál sin guardar el dato en algún lado. |

## Consecuencias

**Positivas:** una conversación de Messenger siempre responde por la Página
correcta, incluso con múltiples Páginas conectadas bajo el mismo tenant.

**Negativas / lo que aceptamos pagar:** conversaciones creadas ANTES de
esta corrección no tienen `fb_page_id` y siguen usando el criterio viejo
(la primera Página de la conexión) hasta que reciban un nuevo inbound que
lo pueble. **Qué haría que revisáramos esto de nuevo:** si aparece un
reporte real de un tenant con múltiples Páginas recibiendo respuestas
cruzadas en una conversación vieja sin inbound reciente.

## Impacto en el código

`alembic/versions/0027_conversations_fb_page_bridge.py`,
`core/db/models.py` (`Conversation.fb_page_id`),
`core/orchestrator/consumer.py` (lo puebla),
`core/gateway/sender.py::_resolve_messenger_credentials` (lo consume),
`tests/integration/test_meta_messenger_admin.py::
test_dos_paginas_bajo_la_misma_conexion_no_se_cruzan_al_responder`.
