## Bienvenida de 5 pasos en el servidor (2026-11-18, HU-AOS-84)

- El recorrido de primer uso pasa a 5 pasos (`TOTAL_PASOS = 5`, SPEC-49): Tu negocio y tipo de agente · Habilidades y formas de pago · Datos del cliente · Datos para probar · Revisar, probar y publicar.
- Lo guardado con 4 pasos se lee con `PASO_DE_4_A_5` (1→1, 2→2, 3→4, 4→5); lo de 9 y lo de 6 (la APK, sin `pasos`) pasan por las tablas de siempre y luego por esa. El `{paso: 5, pasos: 4}` que escribía `dejar_listo` se lee como el paso 2 (Habilidades).
- `POST /admin/onboarding/avanzar` acepta `pasos` 4, 5 o 9, o ninguno; otro total responde 422 y un paso mayor que el total con 4 o 5, 409 `transicion_invalida`. El esquema de OpenAPI sigue siendo `integer` (solo cambia la descripción).
- `dejar_listo` deja el recorrido en el paso 2 en vez del 5 y no retrocede uno que ya va más adelante. `finalizar` queda en el paso 5.
