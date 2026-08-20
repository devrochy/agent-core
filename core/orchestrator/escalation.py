"""Escalamiento a humano — mecanismo NATIVO del core.

Reglas (los plugins pueden disparar más vía MCPHost.trigger_escalation):
- keyword: el cliente pide un humano o usa palabras configuradas.
- guardrail: una validación bloqueó la respuesta dos veces.
- llm_refusal: el modelo se negó a responder.
- plugin:<razon>: disparada por un plugin.

Al escalar SIEMPRE se pausa el bot para esa conversación
(lección de la Fase 18 de agent-sale: el bot seguía respondiendo tras escalar).
"""

from __future__ import annotations

import uuid

from core.db.models import Conversation, Escalation
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.events import ON_ESCALATION_TRIGGERED, event_bus
from core.shared.logger import get_logger

log = get_logger(__name__)

DEFAULT_ESCALATION_KEYWORDS = ["asesor", "humano", "persona real", "reclamo", "queja"]

ESCALATION_REPLY = (
    "Entiendo, ya mismo te comunico con una persona de nuestro equipo. "
    "En un momento te contactan por este mismo chat."
)


async def escalate(conversation_id: uuid.UUID, reason: str, summary: str = "") -> None:
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return
        conversation.status = "escalated"
        conversation.bot_paused = True  # CRÍTICO: el bot deja de responder ya
        session.add(Escalation(conversation_id=conversation_id, reason=reason, summary=summary))

    await record_audit(
        actor="core:escalation",
        action="escalate",
        conversation_id=conversation_id,
        input_data={"reason": reason, "summary": summary},
    )
    await event_bus.emit(ON_ESCALATION_TRIGGERED, conversation_id=conversation_id, reason=reason)
    log.info("conversation_escalated", conversation_id=str(conversation_id), reason=reason)


def matches_escalation_keywords(message: str, configured_keywords: list[str] | None = None) -> bool:
    keywords = configured_keywords or DEFAULT_ESCALATION_KEYWORDS
    lowered = message.lower()
    return any(keyword.lower() in lowered for keyword in keywords)
