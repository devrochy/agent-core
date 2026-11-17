## Bienvenida de 4 pasos (2026-11-17, HU-AOS-81)

- El recorrido de primer uso pasa a 4 pasos (`TOTAL_PASOS = 4`): Tu negocio y tipo de agente · Habilidades y formas de pago · Datos para probar · Revisar, probar y publicar.
- Lo guardado con 9 pasos (SPEC-47) o con 6 (sin `pasos`) se traduce al leerlo; `POST /admin/onboarding/avanzar` acepta `pasos: 4`, `pasos: 9` (un panel en caché) o ninguno (la APK, numeración de 6).
