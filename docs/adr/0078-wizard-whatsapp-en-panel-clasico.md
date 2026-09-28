# ADR-0078 — El wizard de WhatsApp (Embedded Signup/BYOK) permanece en el panel clásico

- **Estado:** aceptado
- **Fecha:** 2026-09-28

## Contexto

El wizard de alta de WhatsApp vive hoy en el panel SSR (`core/admin/views.py` y
`templates/meta_wizard_*`): incluye el popup de **Embedded Signup** de Meta (requiere
el SDK de Facebook en el navegador y ventanas emergentes), el intercambio de token
(`exchange`), el flujo **BYOK** (pegar/inspeccionar/extender token), el registro de
número, el PIN de dos pasos y la carga manual de respaldo (`/conexiones/manual`). Es
un flujo largo con estado en servidor y dependencias del navegador.

El panel nuevo (`solvershub/apps/panel-web`) ya reconstruyó el resto de Conexiones
(catálogo, alta manual por JSON Schema, estado, salud de Meta, números, plantillas y
WhatsApp Web) y accede al clásico a través del proxy `/legacy` con **sesión compartida**
(ADR-0076).

## Decisión

El wizard de WhatsApp (Embedded Signup y BYOK) **no se reconstruye** en el panel
nuevo en esta etapa: se consume desde el panel clásico vía `/legacy`, enlazado desde
el catálogo de Conexiones del panel nuevo.

- El catálogo del panel nuevo enlaza cada tipo `wizard` a su `setup_url` clásica bajo
  el prefijo `/legacy` (p. ej. `/legacy/admin/conexiones/wizard/whatsapp/elegir-entorno`).
- `whatsapp_waweb` es la excepción: su UI nueva (`/conexiones/waweb`) es la única
  superficie (QR, riesgo y reinicio).
- La carga manual de respaldo (`/legacy/admin/conexiones/manual`) se enlaza desde el
  propio diálogo de alta, como salida cuando el asistente falla.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reconstruir el wizard en Next ahora | Duplica un flujo con popup del SDK de Meta, estado en servidor y tokens; riesgo alto y poco valor frente a la sesión compartida |
| Dejar el catálogo sin enlaces al clásico | El usuario no sabría que el asistente existe; se perdería capacidad visible |
| Reescribir completo el panel clásico antes de migrar Conexiones | Bloquea el resto del rediseño por un flujo puntual |

## Consecuencias

**Positivas:** cero riesgo para el alta de WhatsApp; el panel nuevo cubre el 100 % del
resto y enlaza lo que falta con la misma sesión; se libera esfuerzo para el resto de
la migración.

**Negativas / lo que aceptamos pagar:** dos interfaces conviven para el alta de
WhatsApp; el usuario sale del panel nuevo (aunque sin volver a autenticarse); el
wizard depende de la plantilla SSR y de su proxy.

**Qué haría que revisáramos esto:** que el panel clásico se retire (→ reconstruir el
wizard), que Meta cambie el flujo de Embedded Signup (→ reconstruir sobre la API
actual), o que aparezca un segundo consumidor del alta que no sea el panel (→ exponer
el flujo como API). Es el primer ítem de la fase final (reconstrucción de packs y
superficies del clásico).

## Impacto en el código

- `apps/panel-web/components/.../new-connection-dialog.tsx` (enlaces por tipo).
- `apps/panel-web/README.md` y `solvershub/PLAN-DE-PRUEBAS.md` (caso `LG-04`).
- Sin cambios en `core/admin/**` ni en los contratos.
