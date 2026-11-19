## Correos con diseño HTML de marca (2026-11-19)

- Los correos salen ahora como `multipart/alternative`: el texto plano de siempre queda como
  respaldo y se agrega un HTML con la identidad de SolversHub (tokens de `solvershub-panel/brand`:
  navy, azul de acción, Montserrat/Inter). Sin cambios de esquema ni de contratos.
- Plantillas nuevas en `core/gateway/email_templates/`: MJML en `src/` y HTML compilado en
  `html/` (se compila con `scripts/build_email_templates.sh`; el runtime no necesita Node).
- Diseño propio por correo: código de recuperación (`password_reset`), acceso/invitación del
  dueño (`access_invite`), aviso de renovación (`renewal`), informe de pruebas (`qa_report`) y
  el correo libre del asistente (`generic`), que conserva el mismo armazón.
- `send_system_email` y el adaptador SMTP aceptan `html` opcional; si la plantilla fallara, el
  correo sale en texto plano (el diseño nunca tumba un envío).
- El isotipo de SolversHub encabeza los cinco correos (PNG público del sitio de marca, porque
  Outlook/Gmail no renderizan SVG); el wordmark de texto se mantiene al lado.
- El correo de confirmación de cita sigue enviando el enlace de Google Calendar (`{link}`,
  HU-CAL-05), pero el render lo muestra como botón "Agregar a mi calendario" en vez de una URL
  larga en el texto.
- Todo lo que viene de fuera (nombre del negocio, asunto y cuerpo del asistente) se escapa con
  autoescape de Jinja2; los tests cubren el escape y la ausencia de marcas técnicas.
