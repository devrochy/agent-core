## Motivos de escalación legibles (HU-UX-17, SPEC-31)

- `escalate_to_human` recibe `reason` de una lista cerrada (`solicitud_cliente`, `queja`, `fuera_de_alcance`,
  `intentos_fallidos`, `monto_alto`, `otro`) y un `detalle` libre aparte, como en agent-sale. Se guarda
  `tool:<código>`; el detalle va al final del resumen. Un `reason` fuera del catálogo no falla: queda como
  `otro` con el texto como detalle.
- `core/orchestrator/motivos_escalacion.py` normaliza todo `Escalation.reason` (de la tool, de los plugins y del
  sistema: `keyword`, `llm_error`, `llm_refusal`, `guardrail`, `regla:<x>`, `<guardrail>: <motivo>`,
  `persona_forbidden_phrase`, `send_failed`, `turns_unresolved`, `max_iterations`) a un código cerrado y a una
  etiqueta en español. `plugin:<tool>` se muestra como «Acción de <habilidad>». Lo guardado antes con texto
  libre se clasifica como `otro` sin migrar datos.
- `GET /api/v1/admin/escalations` expone `reason_code` y `reason_label` (además de `reason` crudo) y acepta
  `?reason_code=` (filtro en SQL, respeta la paginación; un código fuera del catálogo da 422).
- Cambia el esquema de la tool que ve el modelo: invalida las grabaciones del gate de evals (hay que regrabar).
