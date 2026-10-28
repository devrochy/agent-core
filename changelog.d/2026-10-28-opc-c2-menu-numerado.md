## Opciones directas: «1» o el título contestan un menú numerado (SPEC-40, pista C2, 2026-10-28)

- En los canales de texto con la capacidad `respuesta_numerica` (WhatsApp por QR, chat web, Messenger, Instagram; no Meta, correo ni playground), la respuesta «1» (o «1.», «1)») o el título exacto de una opción de un menú numerado (`option_sends.via='text'`) se resuelve **antes del LLM** como si hubiera tocado el botón. Guardas: bandera `opciones_directas.habilitada` encendida, menú vigente (`expires_at`) y último mensaje saliente de la conversación, coincidencia exacta; cualquier otra cosa sigue al agente como hoy.
- Solo acciones no sensibles: una opción que pide confirmación no se ejecuta por texto (texto fijo `sensible_por_texto`, sin activación ni tool).
- El `opt:` lo firma el servidor; reutiliza el resolver (`Toque.via = "text"`), la idempotencia `dedupe_key` y la auditoría `option_tap`, que ahora lleva `via` (`button` | `text`).
- Sin migración ni cambio de contrato. Con la bandera apagada (por defecto) no cambia nada para nadie.
