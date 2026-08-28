# HU-007 · Tareas programadas de plugins

**Como** developer de plugins, **quiero** declarar cron jobs con un decorador,
**para** tener capacidades proactivas (follow-ups, reportes) sin tocar el core.

## Criterios de aceptación

- [ ] `@register_cron("mod_x", "0 9 * * *")` sobre una corrutina la registra en
      Celery Beat con ese crontab (UTC).
- [ ] La tarea se SALTA automáticamente si el plugin está inactivo (guard del core).
- [ ] Una excepción en el job se loggea y no afecta otros jobs ni el asistente.
- [ ] Expresión cron inválida → error claro al importar el plugin (falla la
      activación, no el runtime).
- [ ] Mensajería proactiva desde jobs usa `send_to_conversation()` y respeta la
      ventana de 24h de WhatsApp (responsabilidad del plugin; documentado y
      ejemplificado en `follow_up_leads` de pack_commerce).
- [ ] `acks_late` en Celery: si el worker muere a mitad de tarea, se re-entrega.
