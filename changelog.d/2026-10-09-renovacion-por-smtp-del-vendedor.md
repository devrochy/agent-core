## El aviso de renovación sale por el SMTP del vendedor (2026-10-09)

- El aviso de renovación por el panel de un negocio del alta automática sale por la integración
  SMTP de su vendedor (canal de correo saliente, ADR-0071). Si el vendedor no tiene una, o el
  negocio no tiene vendedor, sale por el SMTP de la plataforma. `renovacion_avisada` registra el
  `remitente`. Enmienda de ADR-0123.
- El acceso del alta y la recuperación de contraseña siguen por el SMTP de la plataforma.
- BACKLOG: elegir el SMTP por propósito cuando un negocio tiene varias integraciones de correo.
