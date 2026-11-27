## Identidad: Voz de marca en Voz y estilo (2026-11-26)

- `brand_voice` deja el grupo `identidad` del catálogo (SPEC-37) y pasa al grupo `voz`: se edita en
  Configuración › Voz y estilo, junto al tono y la personalidad. El panel mantiene el override para
  funcionar mientras un backend anterior todavía la mande en Identidad.
- La explicación de `brand_voice` dice para qué se usa: entra en cada turno cuando está escrita; si
  queda vacía, el asistente solo tiene el tono y la personalidad.
- `IdentitySettings` devuelve `language` (solo lectura, `es` por defecto) para que el panel pueda
  mostrar el idioma del negocio como dato, sin un selector deshabilitado que parezca roto.
