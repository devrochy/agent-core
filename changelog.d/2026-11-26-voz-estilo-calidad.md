## Voz y estilo: la regla de fallo vive en Calidad (2026-11-26)

- `guardrail_fail_action` pasa del grupo `respuesta` al grupo `calidad` del registro de opciones
  (HU-CFG-12). Es una regla de validación, no de voz: en el panel aparece junto a los reintentos.
- Sin cambios de contrato, endpoints ni almacenamiento: sigue en `behavior_config` y se guarda con
  el mismo `PUT` de identidad.
