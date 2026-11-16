## Bienvenida de 9 pasos (2026-11-16)

- HU-UX-23 (SPEC-47, D-75). El recorrido del onboarding pasa de 6 a 9 pasos (`TOTAL_PASOS = 9`): Tu
  negocio, Identidad, Cómo habla, Horario y reglas, Habilidades, Flujo, Conocimiento y datos, Pruébalo y
  Publicar. Lo guardado lleva `pasos`; un recorrido guardado con 6 (sin esa clave) se lee en su
  equivalente de 9 (`PASO_DE_6_A_9`) y, al escribirse otra vez, queda guardado con los 9 sin perder el
  paso. `POST /admin/onboarding/avanzar` admite pasos del 1 al 9. La APK sigue con sus 6 pasos: recorta
  el paso al suyo.
