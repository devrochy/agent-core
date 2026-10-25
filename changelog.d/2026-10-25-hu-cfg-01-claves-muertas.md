## Limpieza de claves muertas de la configuración (HU-CFG-01, SPEC-37) (2026-10-25)

- `tono` (columna de `app_settings` sin lector) deja de escribirse: el alta de negocio, `agent-core init` y los
  presets ya no lo guardan. `PUT /api/v1/admin/settings/identity` sigue aceptándolo (una APK instalada lo envía)
  pero lo ignora; la lectura lo conserva marcado `deprecated` en el OpenAPI y `IdentityUpdate.tono` pasa a ser
  opcional. El tono real es `persona.tone`; el CLI `configure tone` lo escribe ahí (`cambiar_tono_persona`).
- `DebounceMode` incluye `corto` (espera 3 s), que ya existía en `DEBOUNCE_DELAY_MS` pero no se podía elegir.
- Se corrigen comentarios del modelo que citaban una clave fantasma y se quita `tono` del panel SSR heredado.
- Sin migraciones.
