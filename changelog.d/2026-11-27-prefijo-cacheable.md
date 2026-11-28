## Menos entrada sin caché por turno (2026-11-27)

- «Capacidades activas en este turno» lista solo los nombres de las tools del turno, sin su
  descripción, que ya va en el esquema de funciones. Eran ~930 tokens sin caché por llamada en
  las cintas de evals; ahora son unos pocos. Sin la lista, el modelo llamaba tools que el prompt
  nombra pero la etapa no ofrece.
- El bloque dinámico del turno (fecha y hora, tarjeta, memorias, perfil) ahora viaja detrás
  del historial, justo antes del mensaje del cliente, como un segundo mensaje `system`
  (DeepSeek y demás OpenAI-compatibles). Así el historial de los turnos anteriores entra en la
  caché por prefijo del proveedor. Con Anthropic (BYOK) el bloque se queda en `system`, como
  hasta ahora. La fecha sigue al minuto (adenda 2026-11-27 de ADR-0117).
- Estimación sobre las 147 llamadas grabadas: la entrada sin caché baja ~59 % (de 2.010 a
  ~816 tokens por llamada). Se mide en test con Costos › por agente.
- Grabaciones de los journeys del gate regrabadas con LLM real (OK del dueño): el ancla del
  bloque entra en la clave. El golden no cambia.
