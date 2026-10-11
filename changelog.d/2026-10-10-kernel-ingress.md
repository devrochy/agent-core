## Políticas de entrada en el kernel del Coordinador (2026-10-10)

- Las políticas del mensaje que entra pasan del consumer a `core/kernel/ingress.py` (`politicas_de_entrada`): conexión
  pausada (ADR-0122), kill-switch o conversación escalada, palabras de escalamiento y horario de atención. El debounce
  usa la misma guarda de conexión pausada al disparar (`conexion_pausada`). HU-AOS-10, parte 2.
- Sin cambio de conducta: mismo orden, mismas respuestas y mismas decisiones en `core_decisions`. La velocidad de
  respuesta (inmediato o debounce) sigue en el consumer.
