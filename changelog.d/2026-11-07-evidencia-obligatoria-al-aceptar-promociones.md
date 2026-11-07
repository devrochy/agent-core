## Aceptar promociones exige evidencia con cualquier forma de obtenerlo (2026-11-07)

- Decisión del dueño (2026-11-07), ADR-0150 §6. `POST /admin/leads/{id}/consentimiento` con `estado: "otorgado"` ahora responde 400 `evidencia_requerida` («Cuenta cómo lo obtuviste.») si la evidencia viene vacía o solo con espacios, con «En el local», «Por teléfono», «Por escrito» y «Otro» (antes solo «Otro» la exigía). `revocado` no la exige.
- Orden de las comprobaciones: 403 por rol (solo propietario o master), luego la evidencia, luego el 409 `baja_del_cliente`. Sin migración; el contrato (schemas) no cambia, solo la descripción de la ruta en `docs/openapi.json`.
- Pendiente fuera de este PR: comprobar que el panel y la APK pidan siempre la evidencia al marcar «Aceptó recibir promociones»; si no, el equipo verá el 400 con el mensaje de arriba.
