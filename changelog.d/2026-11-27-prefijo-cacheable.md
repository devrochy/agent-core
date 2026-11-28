## Menos entrada sin caché por turno (2026-11-27)

- El prompt del sistema ya no repite las tools del turno («Capacidades activas en este
  turno»): el modelo las ve con su descripción en el esquema de funciones. Eran ~930 tokens
  sin caché por llamada en las cintas de evals.
- El bloque dinámico del turno (fecha y hora, tarjeta, memorias, perfil) ahora viaja detrás
  del historial, justo antes del mensaje del cliente, como un segundo mensaje `system`
  (DeepSeek y demás OpenAI-compatibles). Así el historial de los turnos anteriores entra en la
  caché por prefijo del proveedor. Con Anthropic (BYOK) el bloque se queda en `system`, como
  hasta ahora. La fecha sigue al minuto (adenda 2026-11-27 de ADR-0117).
- Estimación sobre las 147 llamadas grabadas: la entrada sin caché baja ~65 % (de 2.010 a
  ~700 tokens por llamada). Se mide en test con Costos › por agente.
- Las grabaciones de los journeys del gate quedan desactualizadas (el ancla del bloque entra
  en la clave): se regraban con LLM real y con el OK del dueño. El golden no cambia.
