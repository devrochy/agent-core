## Ciclo de vida seguro de las conexiones de Meta (2026-10-08)

- HU-CONN-03, ADR-0122. **Desactivar pausa de verdad.** Lo que entra se guarda sin IA y se avisa a los dueños con un
  push al día. No sale nada por la conexión, y lo que estaba en cola falla sin escalar. Una BYOK pausada ya no da 401:
  su entrada se verifica y se guarda.
- **Sin respaldo silencioso.** Si la conexión de la conversación no puede enviar, la respuesta no sale desde la
  conexión primaria ni desde el número de la plataforma.
- **Activar** solo reactiva una conexión pausada, y solo con el diagnóstico verificado en el acto.
- **Eliminar sin perder historia.** Migración 0063: números y WABAs con `deleted_at`, único parcial del número más un
  único por negocio, y plantillas únicas por negocio. Plantillas y `pricing_daily` ya no se borran en cascada. Los
  tokens se revocan, y en Embedded Signup se desuscribe la app de la plataforma de la WABA. Reconectar el mismo
  número o la misma WABA revive las filas.
- Ninguna acción del panel opera sobre una conexión eliminada.
- Los webhooks de plantillas buscan por negocio en vez de usar el resolvedor global con `LIMIT 1`.
