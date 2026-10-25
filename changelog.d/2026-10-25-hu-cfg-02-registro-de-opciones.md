## Registro de opciones de configuración del negocio (HU-CFG-02, SPEC-37, ADR-0166) (2026-10-25)

- Nuevo `core/config_registro/`: un modelo `Opcion` (`extra="forbid"`) y las 31 opciones de Identidad y comportamiento
  (columnas de `AppSettings`, `behavior_config`, `escalation_config` y persona del spec), cada una con etiqueta,
  explicación en español, valores con etiqueta, rango, valor por defecto, dependencia, quién la edita y dónde se guarda.
  Es una vista sobre el almacenamiento actual: sin tablas ni migraciones.
- Los valores salen del runtime (`DEBOUNCE_DELAY_MS`, `CAPS`, `TOGGLES`, umbrales de dificultad, política de
  WhatsApp Web, contrato `Persona`); los guardrails apagables se calculan por negocio con los packs habilitados.
- Nuevo `GET /api/v1/admin/settings/catalog` (sección `settings`): el master recibe las 31; el resto, las 26 que no son de
  WhatsApp Web. Incluye `version` (huella del contenido) para que la APK sepa si su copia sigue vigente.
- Prueba de consistencia con `ast`: toda clave literal que el código lee de `behavior_config`/`escalation_config` debe estar
  registrada, y toda registrada debe tener lector. Otra prueba vigila que el runtime de turnos no importe el registro (las
  etiquetas son texto para personas, nunca prompts).
- ADR-0166.
