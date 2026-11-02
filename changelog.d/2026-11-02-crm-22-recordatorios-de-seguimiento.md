## Recordatorios de seguimiento y resumen diario de ventas (HU-CRM-22, 2026-11-02)

- **Aviso al vencer.** Nuevo trabajo `pack_commerce.recordar_seguimientos` (cada 5 minutos, sin LLM y sin mensajes al
  cliente): cuando llega la hora de un seguimiento que una persona programó (HU-CRM-21) crea el aviso
  `commerce.seguimiento_vencido` («Seguimiento: <cliente>», con la nota y la hora) que abre la ficha del cliente.
- **Una sola vez.** Sin tabla ni migración: la clave de deduplicación del aviso es `seguimiento:<lead>:<instante>`, así que
  correr el trabajo de más o reiniciarlo no repite nada y, si la fecha cambia, vuelve a avisar por la fecha nueva.
- **Reglas.** Espera al horario de atención del negocio (`escalation_config.business_hours`; sin horario, siempre abierto);
  no avisa de un lead `convertido` o `perdido`; un seguimiento vencido hace más de 4 días ya no genera aviso (sigue en
  «Vencidos»); un lead sin fecha, como los que crea el agente, no genera nada.
- **Resumen diario.** `commerce.resumen_ventas` («Tu día de ventas»): una vez al día, desde la hora configurada en la zona
  del negocio y hasta 4 horas después, con los seguimientos de hoy, los vencidos y los leads nuevos de las últimas 24 h
  (mismas reglas que los filtros de la lista). Sin nada que decir, no sale. Abre la lista filtrada por «Vencidos» o «Hoy».
- **Configuración.** Tres campos nuevos en la configuración del pack, encendidos por defecto: `seguimiento_recordatorios`,
  `seguimiento_resumen_diario` y `seguimiento_resumen_hora` (0–19, por defecto 8).
- **Contrato.** El destino de un aviso (`NotificationTarget`) gana el campo opcional `filtro` (`clave:valor`, p. ej.
  `seguimiento:vencidos`); aditivo: un cliente que no lo conozca abre la lista sin filtrar. `docs/openapi.json` actualizado.
