## Una sola validación de la configuración (HU-CFG-03, SPEC-37) (2026-10-25)

- Nuevo `core/config_limites.py` (módulo hoja): el rango de cada opción numérica se escribe una sola vez. De ahí lo leen el
  registro de opciones, `IdentityUpdate`, `save_identity`, el panel SSR, `loop.py` y la política de WhatsApp Web (antes los
  umbrales estaban escritos cuatro veces).
- `PUT /api/v1/admin/settings/identity` rechaza con 422 (campo marcado y lista de valores válidos) un `debounce_mode`,
  `split_style`, `persona_tone` o `persona_emojis` desconocido. Antes `debounce_mode` aceptaba cualquier texto y se comportaba
  como «sin espera», y `split_style` caía en silencio a `un_mensaje`. Un campo de voz vacío sigue significando «no cambiar».
- `auto_escalate_after_turns`: la API aceptaba hasta 1000 pero el runtime siempre recortó a 20 (ADR-0051). Ahora el rango de
  la API, del panel SSR y del runtime es 0–20 y un valor mayor responde 422. Cambia `docs/openapi.json` (`maximum` 1000 → 20).
- La lectura del runtime sigue siendo tolerante: un valor guardado fuera de rango, no numérico o `null` se recorta o cae al
  defecto en vez de tumbar el turno (`leer_entero`).
- Un preset con una clave de `app_settings` desconocida o un `split_style` inválido falla al cargar nombrando el valor.
- `GET /api/v1/admin/settings` devuelve los valores como los aplica el runtime: una fila vieja fuera de rango se recorta (umbrales,
  reintentos, `auto_escalate_after_turns`, calidez, longitud) y un `debounce_mode` o `split_style` desconocido se muestra como lo trata el
  runtime (`inmediato` y `pocos_cortos`). Así el panel y la APK, que reenvían lo que leyeron, nunca dejan al dueño sin poder guardar
  por un 422 que no puede ver ni corregir. La calidez 0 ya no se muestra como 3.
