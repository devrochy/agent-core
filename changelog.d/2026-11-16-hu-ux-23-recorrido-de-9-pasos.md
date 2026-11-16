## Bienvenida de 9 pasos (2026-11-16)

- HU-UX-23 (SPEC-47, D-75). El recorrido del onboarding pasa de 6 a 9 pasos (`TOTAL_PASOS = 9`): Tu
  negocio, Identidad, Cómo habla, Horario y reglas, Habilidades, Flujo, Conocimiento y datos, Pruébalo y
  Publicar. Lo guardado lleva `pasos`; un recorrido guardado con 6 (sin esa clave) se lee en su
  equivalente de 9 (`PASO_DE_6_A_9`) y, al escribirse otra vez, queda guardado con los 9 sin perder el
  paso. `POST /admin/onboarding/avanzar` admite pasos del 1 al 9. La APK sigue con sus 6 pasos: recorta
  el paso al suyo.
- `AvanzarIn` suma `pasos` (opcional): los clientes de 9 pasos mandan `pasos: 9`; sin él, el paso viene
  del asistente de 6 (panel o APK anteriores) y se guarda traducido. Así desplegar el backend antes que
  el panel, o una APK sin OTA, no deja pasos corridos guardados en la base.
