## Etapa del lead confiable y con historia (2026-10-30)

- **HU-CRM-18 / ADR-0181.** La etapa del lead de venta pasa por una sola función
  (`pack_commerce.etapas.cambiar_etapa`) que valida contra las cinco etapas, aplica las reglas por actor y
  registra cada cambio (anterior, nueva, agente|persona|sistema, cuándo) en la tabla nueva
  `pack_commerce_lead_stage_changes` (migración `crm_0015`, con RLS e idempotencia).
- `actualizar_lead` rechaza una etapa inválida con la lista de las válidas y no guarda nada del llamado;
  `calificar_lead` actualiza el score pero ya no devuelve a «calificado» a un lead contactado, convertido o
  perdido; solo una persona (panel o APK) reabre un lead «perdido»; el seguimiento automático deja a «sistema»
  como actor y no pisa un cambio hecho a mano mientras salía el mensaje.
- La línea de tiempo de la ficha muestra «Etapa: A → B» con quién lo hizo, y la ficha trae `etapa_desde`
  y `etapa_desde_aproximada` en `extras` (los leads anteriores usan su última actividad, marcada como
  aproximada). Sin cambio en `docs/openapi.json`.
