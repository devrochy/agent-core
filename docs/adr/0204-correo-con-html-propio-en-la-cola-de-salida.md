# ADR-0204 — Correo con HTML propio en la cola de salida

- **Estado:** aceptado
- **Fecha:** 2026-11-19
- **Usa:** ADR-0071 (canal de correo saliente), ADR-0098 (correo de sistema), ADR-0032 (cola de salida).

## Contexto

Los correos salían solo como texto y el canal SMTP les armaba un HTML genérico al enviar. La
confirmación de una cita necesita una ficha con los datos de la cita (título, fecha, hora y
negocio) y el botón «Agregar a mi calendario». El pack de agenda encola texto: el canal no sabe
que es una cita ni de dónde sacar esos datos, así que el diseño se quedaba en un párrafo con la
URL del calendario.

Alternativas:

| Opción | Por qué no |
|---|---|
| Que el canal detecte el enlace de calendario y rearme el HTML | Sin una ficha real: el canal no tiene los campos estructurados (título, fecha, hora) |
| Tabla de correos paralela | Duplica la cola, los reintentos y la DLQ (misma razón que ADR-0071) |
| Columna `messages.html` nullable | Una fila más por correo que ya existe; el texto sigue siendo el respaldo |

## Decisión

1. **`messages.html`** (`Text`, nullable) viaja con el mismo `Message` de la cola. `content`
   sigue llevando el texto plano; el HTML es presentación.
2. **`queue_outbound_email(..., html=None)`** lo persiste. La tarea de salida usa
   `adapter.send_text_with_html(...)` cuando el mensaje lo trae.
3. **`OutboundAdapter.send_text_with_html`** delega por defecto en `send_text`: los canales que
   no manejan HTML lo ignoran. El adaptador SMTP lo envía como `multipart/alternative` (texto +
   HTML).
4. **El pack de agenda** arma la ficha con `core.gateway.email_templates.html_cita`, la misma
   marca y las mismas piezas que los correos de plataforma, y la encola. Si la plantilla falla,
   `html` queda vacío y el correo sale como antes.
5. **Plantillas MJML compiladas y versionadas** en `core/gateway/email_templates/` (ver su
   `README.md`): el runtime no depende de Node.

## Consecuencias

- La ficha de una cita se ve igual en la confirmación, el reagendamiento y la cancelación.
- No hay modelo de envío paralelo ni migración de datos: la columna es aditiva y nullable.
- El HTML usa los campos estructurados de la cita, **no** el `email_body_*` que personalice el
  negocio; ese texto sigue siendo el respaldo plano. Si un negocio necesita su copy también en
  HTML, se extiende el contexto de la plantilla (decisión posterior, no implícita).
- El HTML de un pack viaja por la cola como presentación, nunca como instrucción (invariante 7).
- Un pack nuevo que quiera su propio diseño agrega su `html_*` en `email_templates` y sigue este
  mismo camino.

## Impacto en el código

- `core/db/models.py` y migración `0149` (columna nullable; sin RLS nueva: `messages` ya la tiene).
- `core/gateway/outbound_email.py`, `core/tasks/outbound_send.py`,
  `core/gateway/channels/base.py` y `core/gateway/channels/email.py`.
- `packages/pack-calendar/pack_calendar/skills.py` (ficha de cita).
- `core/gateway/email_templates/` (plantilla `appointment`).
