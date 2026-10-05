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
from datetime import datetime

from core.db.models import Conversation, Escalation
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.events import ON_ESCALATION_TRIGGERED, event_bus
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.shared.test_mode import is_test_channel

log = get_logger(__name__)

DEFAULT_ESCALATION_KEYWORDS = ["asesor", "humano", "persona real", "reclamo", "queja"]

ESCALATION_REPLY = (
    "Entiendo, ya mismo te comunico con una persona de nuestro equipo. "
    "En un momento te contactan por este mismo chat."
)

# Respuesta neutral al agotar el guardrail con `guardrail_fail_action=neutral`
# (ADR-0092). Respaldo determinista de la sección `verifying` de `respuestas.md`.
VERIFYING_REPLY = "Dejame verificar eso y te confirmo en un momento, por favor."


async def escalate(conversation_id: uuid.UUID, reason: str, summary: str = "") -> None:
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return
        # Conversación del playground (channel "test"): no es atención real. No
        # se crea `Escalation`, no se pausa el bot y no se emite alerta -- sería
        # contaminar la cola del cliente con una prueba del panel.
        if is_test_channel(conversation.channel):
            log.info("escalation_skipped_test", conversation_id=str(conversation_id), reason=reason)
            return
        conversation.status = "escalated"
        conversation.bot_paused = True  # CRÍTICO: el bot deja de responder ya
        session.add(
            Escalation(
                tenant_id=get_current_tenant_id(),
                conversation_id=conversation_id,
                reason=reason,
                summary=summary,
            )
        )

    await record_audit(
        actor="core:escalation",
        action="escalate",
        conversation_id=conversation_id,
        input_data={"reason": reason, "summary": summary},
    )
    await event_bus.emit(ON_ESCALATION_TRIGGERED, conversation_id=conversation_id, reason=reason)

    # Push a los dueños del negocio (ADR-0099): fire-and-forget por Celery.
    try:
        from core.tasks.push_tasks import TITULO_ESCALACION, notify_owners_push

        tenant = get_current_tenant_id()
        if tenant is not None:
            notify_owners_push.delay(
                str(tenant),
                TITULO_ESCALACION,
                "Un cliente pidió atención humana y la conversación quedó escalada.",
                {"type": "core.escalation", "conversationId": str(conversation_id), "reason": reason},
            )
    except Exception:
        log.warning("push_escalacion_fallo", conversation_id=str(conversation_id), exc_info=True)

    log.info("conversation_escalated", conversation_id=str(conversation_id), reason=reason)


def matches_escalation_keywords(message: str, configured_keywords: list[str] | None = None) -> bool:
    keywords = configured_keywords or DEFAULT_ESCALATION_KEYWORDS
    lowered = message.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def within_business_hours(business_hours: dict | None, now: datetime | None = None) -> bool:
    """True si `now` cae dentro de las franjas configuradas (ADR-0051).

    `business_hours` = {"tz": "America/Bogota", "ranges": [{"day": 0-6, "from":
    "08:00", "to": "18:00"}]}. Sin config → siempre abierto (fail-open: no bloquear
    la atención por una config ausente). Una tz inválida cae a UTC y se loggea.
    """
    if not isinstance(business_hours, dict):
        return True
    ranges = business_hours.get("ranges")
    if not ranges or not isinstance(ranges, list):
        return True
    from datetime import UTC
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_name = business_hours.get("tz") or "UTC"
    try:
        tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("business_hours_tz_invalida", tz=tz_name)
        tz = ZoneInfo("UTC")
    local = (now or _dt.now(UTC)).astimezone(tz)
    # `weekday()`: 0=lunes … 6=domingo. Es el mismo criterio del contrato
    # (BusinessHourRange.day).
    weekday = local.weekday()
    hhmm = local.strftime("%H:%M")
    for rng in ranges:
        # JSONB libre: un elemento malformado no debe tumbar el turno (fail-open).
        if not isinstance(rng, dict):
            continue
        try:
            day = int(rng.get("day"))
        except (TypeError, ValueError):
            continue
        if day != weekday:
            continue
        start = _normalize_hhmm(str(rng.get("from") or ""))
        end = _normalize_hhmm(str(rng.get("to") or ""))
        # Rangos que cruzan medianoche (start > end) no se soportan (se ignoran):
        # el admin los parte en dos franjas.
        if start and end and start <= hhmm < end:
            return True
    return False


def _normalize_hhmm(value: str) -> str:
    """Normaliza "8:00" → "08:00" para que la comparación lexicográfica sea válida."""
    hh, _, mm = value.strip().partition(":")
    if not (hh.isdigit() and mm.isdigit()):
        return ""
    return f"{int(hh):02d}:{int(mm):02d}"
