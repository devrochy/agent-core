## Políticas de entrada: el escalamiento no se pierde si algo falla (2026-10-10)

- Palabra de escalamiento: la conversación se escala antes de cargar el texto de respuesta. Si el
  `AgentSpec` no carga (p. ej. BD caída), se escala igual y se responde el texto fijo de escalamiento.
  Antes quedaba la decisión `escalated` en `core_decisions` sin escalamiento real, y el reintento del
  consumer no lo rehacía (el inbound ya guardado se ve como duplicado).
- Si escalar falla, se reintenta una vez tras 0,5 s. Si vuelve a fallar, el turno queda `failed` en
  `turn_outcomes`, se registra `escalation_keyword_failed`, se avisa a los dueños por push y no se le
  responde nada al cliente (no se promete una persona que no viene).
- Si el aviso al cliente no sale después de escalar, el turno sigue `escalated` (antes quedaba `failed`).
- Fuera de horario: si el envío del mensaje configurado lanza una excepción, se escala como cuando
  devuelve `False` (antes la excepción cortaba el turno sin escalar).
- Los logs nuevos registran solo el tipo de error: el texto de un error de SQLAlchemy trae el mensaje
  del cliente entre sus parámetros.
