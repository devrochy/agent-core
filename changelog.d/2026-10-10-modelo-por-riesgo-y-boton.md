## Modelo por riesgo y confirmación con botón (2026-10-10)

- HU-AOS-09, todo detrás de `tenants.aos_procedures` (con la bandera apagada no cambia nada).
- Una etapa `risk: high` usa el modelo con thinking (`KernelSpec.modelPolicy.highRiskStage`),
  decidido una vez por turno. El razonamiento de DeepSeek vuelve en las vueltas siguientes. Si la
  llamada falla, el turno sigue con el modelo de siempre. Queda en `usage_ledger.result.politica_modelo`
  (ADR-0132).
- El reintento tras un rechazo del verificador usa el respaldo del agente si es de la misma familia
  de modelo; si no, se anota `respaldo_no_disponible`.
- Cuando el turno pide una confirmación, el mensaje lo arma el kernel con los argumentos que va a
  ejecutar (no con el texto del modelo). En WhatsApp, con la ventana abierta y si la etapa lo pide,
  va con el botón "Confirmar" firmado; en los demás canales, como texto.
- Migración `0082`: `messages.interactive` (payload del mensaje interactivo en la cola de salida).
- Clasificador de confirmación escrita, determinista y conservador, medido con un set etiquetado
  (precisión 1.0). El "sí" a secas sigue sin confirmar.
