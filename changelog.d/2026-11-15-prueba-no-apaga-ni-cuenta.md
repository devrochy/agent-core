## La prueba no apaga módulos ni cuenta en las estadísticas (2026-11-15)

- Un fallo de una tool durante una prueba (canal `test`: la prueba del panel y los evals) ya no apaga el pack del negocio (ADR-0024). El turno responde con el error y se registra `plugin_error_en_prueba`; las conversaciones reales siguen con el pack habilitado.
- «Clientes nuevos» del Resumen de Ventas ya no cuenta el cliente sintético de la prueba del panel (`pack_commerce/resumen.py`).
- El dashboard SSR `/admin` ya no cuenta los mensajes de la prueba en «mensajes 7 días», como `/api/v1` (`core/admin/views.py`).
- `/admin/connections/{id}/a2a/calls` ya no lista llamadas de la prueba del panel (`core/api/routers/a2a.py`).
