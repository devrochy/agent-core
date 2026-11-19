# Plantillas de correo

Los correos de SolversHub viajan como `multipart/alternative`: texto plano (respaldo,
`cuerpo` que arma cada emisor) y HTML (diseño, estas plantillas).

## Editar el diseño

1. Editar el `.mjml` en `src/` (fuente de verdad).
2. Compilar:

   ```bash
   bash scripts/build_email_templates.sh
   ```

   El script usa `npx mjml@5` con `minify` y `validationLevel=strict`. Requiere Node 20+.
3. Commitear el `.mjml` **y** el `.html` generado en `html/`. El runtime no compila ni
   depende de Node: sirve el HTML versionado.

## Marca

- El isotipo es una imagen remota: `https://www.solvershub.site/logo.png` (PNG, no SVG:
  Outlook ni Gmail renderizan SVG). Va con `alt=""` porque el wordmark de texto está al lado;
  si el cliente bloquea imágenes, el correo sigue leyéndose.
- El correo del asistente (`generic`) detecta el enlace de "agregar a mi calendario" que arma
  el pack de agenda y lo muestra como botón, en vez de la URL larga.

## Reglas de MJML con Jinja2

- Las variables (`{{ nombre }}`) y los bloques (`{% if %}`) van **dentro de componentes**
  (`mj-text`, `mj-button`, `mj-title`, `mj-preview`).
- Jinja **no** puede envolver componentes MJML: el texto suelto entre secciones se
  descarta al compilar. Para condicionar una sección completa se usan etiquetas
  `mj-raw` *entre* secciones (ver `qa_report.mjml`).
- Nada de `{{ … }}` dentro de atributos `style="…"`: el minificador los interpreta como
  CSS y falla. Los colores por estado se pre-renderizan en Python (`_fallas_html`).
- El contenido dinámico se escapa con autoescape de Jinja2 (`__init__.py`): lo que viene
  de fuera (nombre del negocio, cuerpo del asistente) es dato, no instrucción.

## Plantillas

| Archivo | Correo | Emisor |
|---|---|---|
| `password_reset.html` | Código de recuperación | `core/admin/password_reset_service.py` |
| `access_invite.html` | Acceso/invitación del dueño | `core/alta_acceso.py`, `core/provisioning/owner.py` |
| `renewal.html` | Aviso de renovación de plan | `core/billing/renovacion.py` |
| `qa_report.html` | Informe de pruebas | `core/qa/service.py` |
| `generic.html` | Correo del asistente (texto libre) | `core/gateway/channels/email.py` |

## Probar

```bash
pytest tests/test_email_templates.py -q
```
