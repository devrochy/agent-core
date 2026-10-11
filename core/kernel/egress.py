"""Salida del turno: un mensaje por turno (HU-AOS-10, design §3.5).

Lo que hacían igual el consumer (modo inmediato) y el debounce al terminar el loop: encolar la
respuesta y, si no llegó al canal y el turno no había escalado, escalar a una persona para no
perder al cliente. El status del turno pasa a `escalated` en ese caso. `replied` significa
encolado, no entregado: la entrega la confirma después el estado del mensaje.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.kernel.decisions import marcar_escalado


async def entregar(conversation_id: uuid.UUID, result: Any) -> None:
    # Imports en la llamada: los tests parchean `core.gateway.sender.send_to_conversation`
    # y `core.orchestrator.escalation.escalate`.
    from core.gateway.sender import send_to_conversation
    from core.orchestrator.escalation import escalate

    if result is None or not result.text:
        return
    confirmacion = getattr(result, "confirmacion", None)
    if confirmacion is not None:
        # HU-AOS-09: la confirmación ES el mensaje del turno (`result.text` es su versión de
        # texto). Con botón solo si la etapa lo pide; el sender decide si el canal lo soporta.
        delivered = await _entregar_confirmacion(conversation_id, result.text, confirmacion)
    else:
        delivered = await send_to_conversation(conversation_id, result.text, image_url=result.image_url)
    if not delivered and not result.escalated:
        # El turno se generó pero no llegó al canal: escalar a humano para no perder al
        # cliente (incidente de envío silencioso detectado en producción).
        await escalate(conversation_id, reason="send_failed", summary=result.text[:200])
        marcar_escalado()


async def _entregar_confirmacion(conversation_id: uuid.UUID, texto: str, confirmacion: Any) -> bool:
    from core.gateway.sender import send_interactive_to_conversation, send_to_conversation

    if confirmacion.modo != "interactive_button":
        return await send_to_conversation(conversation_id, texto)
    c = confirmacion.confirmacion
    return await send_interactive_to_conversation(conversation_id, c.interactivo(), fallback_text=texto)
