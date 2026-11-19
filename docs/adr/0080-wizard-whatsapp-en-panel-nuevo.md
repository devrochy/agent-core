# ADR-0080 — El wizard de WhatsApp (Embedded Signup/BYOK) se reconstruye en el panel nuevo

- **Estado:** aceptado · **implementado** (Embedded Signup en el panel nuevo: [ADR-0128](0128-embedded-signup-real.md),
  HU-SH-05, 2026-10-09)
- **Fecha:** 2026-09-29
- **Supera a:** ADR-0078

## Contexto

ADR-0078 decidió que el wizard de alta de WhatsApp viviera en el panel SSR clásico y
se consumiera desde el panel nuevo vía `/legacy`. El propietario del producto decidió
posteriormente **completar la paridad total** del panel nuevo: ninguna superficie debe
obligar a saltar al clásico, y las herramientas que aún viven allí (asistente, DLQ,
plantillas globales, requisitos de Meta, producción, webhooks, wizard de WhatsApp y
OAuth de Google del pack Calendario) se reconstruyen en el panel nuevo y **se retiran
del backend clásico** al cerrar cada pieza.

El wizard clásico incluye: elección de entorno, **Embedded Signup** de Meta (SDK de
Facebook en el navegador + popups), intercambio de token (`exchange`), flujo **BYOK**
(pegar/inspeccionar/extender token), registro del número, PIN de dos pasos y la carga
manual de respaldo.

## Decisión

El wizard de WhatsApp se **reconstruye en el panel nuevo**:

- **API** (`/api/v1`): estado del wizard, `exchange`, BYOK (guardar/inspeccionar/extender),
  registro de número, PIN de dos pasos y alta manual. Los handlers del clásico se retiran
  una vez validada la paridad.
- **Panel** (`/conexiones/whatsapp/...`): pantallas de *elegir entorno* y *wizard* con el
  SDK de Facebook (Embedded Signup) cargado en el navegador.
- **Credenciales**: se leen del entorno (`META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID`,
  `WHATSAPP_VERIFY_TOKEN`). Hasta que estén configuradas, la construcción y su validación
  se hacen con **mocks** (el checklist de conexión real queda documentado).
- El catálogo de Conexiones del panel deja de enlazar `setup_url` clásicas para este tipo;
  el enlace `/legacy` se elimina al cerrar la pieza.

Mismo criterio para el **OAuth de Google del pack Calendario**: el `start`/`callback`
pasa al API del panel (las credenciales son por tenant, en la config del pack) y se
retira el router OAuth clásico.

## Consecuencias

- El panel nuevo queda como **única** superficie de operación; el clásico se retira por
  secciones conforme se migran (ADR-0076).
- La validación end-to-end del wizard y del OAuth requiere credenciales externas
  (Meta App con Embedded Signup y cliente OAuth de Google con el `redirect_uri` del API);
  con mocks se valida el flujo completo salvo la llamada real a Meta/Google.
- ADR-0078 queda **superado** y se mantiene solo como registro histórico.

