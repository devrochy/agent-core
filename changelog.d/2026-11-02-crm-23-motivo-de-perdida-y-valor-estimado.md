## Motivo de pérdida y valor estimado del lead (HU-CRM-23, 2026-11-02)

- **Perder un lead pide motivo.** Pasar un lead a «perdido» (desde la ficha del panel o la app, o con la tool `actualizar_lead`
  del agente) exige un motivo del catálogo cerrado (`precio`, `sin_respuesta`, `compro_en_otro_lado`, `no_era_para_el`,
  `otro`) y admite una nota de hasta 300 caracteres. Sin motivo válido no cambia nada y el mensaje lista los permitidos.
  La tool ofrece `loss_reason`/`loss_note` (opcionales en el esquema, obligatorios al perder).
- **Reabrir.** Reabrir un perdido limpia el motivo vigente; el motivo de la pérdida queda en la historia de etapas y en la línea
  de tiempo («Motivo: Precio — nota»). Un perdido anterior se muestra como «Sin motivo registrado» y se puede anotar después.
- **Valor estimado.** Campo opcional del lead (`numeric(12,2)`, nunca negativo, en la moneda del negocio) editable desde la
  ficha; se escribe como número simple, sin separador de miles. Nuevo `tipo="numero"` en los campos editables del lead.
- **Lista.** Filtros `motivo_perdida` y `valor` (tramos fijos, con contadores) y órdenes `orden=valor` / `orden=valor_desc`
  con los leads sin valor siempre al final; columnas `motivo_perdida` y `valor_estimado`.
- **Migración `crm_0019`** (aditiva; CHECK creados `NOT VALID` y validados; `lock_timeout` 5 s). Reversa solo sin datos.
- **Grabaciones del LLM.** El esquema de `actualizar_lead` cambió: las 9 grabaciones que incluyen la tool quedan desactualizadas
  hasta regrabarlas con `scripts/gate_evals.sh record` (~0,01 USD, con OK del dueño).
