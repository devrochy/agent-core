## Fallas de la primera medición de pass^4 de los procedimientos (2026-10-18)

- `pack_calendar`: el `calendar_completion_check` ya no lee como horario libre las formas genéricas
  ("tenemos disponibilidad", "hay cupos", "hay espacio", "queda libre") sin un dato de agenda en la misma
  oración (hora, día, fecha, cita, demo…). En un negocio que vende y agenda, "¡Sí, tenemos disponibilidad! La
  hamburguesa…" bloqueaba dos veces y escalaba el pedido. Con un dato de agenda sigue bloqueando.
- Loop: al escalar por un guardrail o por el verificador queda una fila `core:guardrail / guardrail_escalated`
  en `audit_log` con el identificador y la etiqueta de la afirmación (sin texto del borrador), también en el
  canal `test`.
- Journeys: el intento que termina en «relevo» dice el motivo (`guardrail=…`, `verificador=…`) y el reporte
  trae `runtime` con lo pedido y las banderas efectivas del negocio (`aos_runtime`, `aos_procedures`).
- Set `procedimientos`: `verifica` de `pack_commerce_order_items` con `"orden": "id"` (no tiene `created_at`) y
  `$no_nulo` como objeto en `03` y `04`. El test de journeys rechaza un operador escrito como string.
