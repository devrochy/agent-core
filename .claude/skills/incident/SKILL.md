---
name: incident
description: >
  Runbook de triage cuando un cliente reporta que el agente no responde, responde mal o
  el sistema está caído. Usar ante "se cayó", "no está respondiendo", "el cliente reporta",
  "está contestando cualquier cosa".
---

Incidente: `$ARGUMENTS`. Diagnostica en este orden y no saltes pasos.

## 1. Alcance (2 minutos)
¿Un tenant, varios, o todos? ¿Un canal o todos? ¿Desde cuándo?
Si son todos → infraestructura. Si es uno → configuración o canal.

## 2. Los cinco sospechosos habituales, en orden de frecuencia
1. **Token o webhook del canal** expirado o revocado por Meta.
2. **Cola atascada**: mensajes en la DLQ, workers caídos, lock de conversación huérfano.
3. **Release reciente**: ¿hubo despliegue en las últimas 24 h? Rollback primero, diagnóstico después.
4. **Tool externa caída**: MCP del cliente o API de terceros sin circuit breaker abierto.
5. **Presupuesto agotado**: límite del plan o `budgetUsdPerConversation` cortando turnos.

## 3. Qué mirar
- Trazas del tenant en la última hora: ¿llegan mensajes al gateway?
- `usage_ledger`: ¿turnos con `outcome` anómalo o costo cero?
- Profundidad de la DLQ por tenant.
- `last_health_check` de los MCP del tenant.

## 4. Mitigar antes de entender
Restaurar servicio primero: rollback, reencolar la DLQ, o desviar a bandeja humana.
El diagnóstico completo va en el postmortem, no en la crisis.

## 5. Cerrar
- Mensaje al cliente: qué pasó, qué se hizo, qué evita que vuelva. Sin jerga.
- Postmortem en `docs/incidents/YYYY-MM-DD-<slug>.md`.
- **Toda causa raíz produce un test, un hook o una alerta.** Si no produce ninguno,
  el incidente no está cerrado.