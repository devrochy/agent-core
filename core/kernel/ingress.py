"""Políticas deterministas del turno antes de gastar LLM (HU-AOS-10, design §3.5).

Primera parte del traslado: las políticas del preámbulo del loop (conversación pausada o
escalada, cuota del período y presupuesto agotado), con el mismo orden, las mismas lecturas y
los mismos textos que antes. Cada bloqueo deja su decisión en `core_decisions`
(`core.kernel.decisions`). Las políticas del consumer (pausa de ADR-0122, kill-switch, palabras
de escalamiento, horario y velocidad) siguen en `core/orchestrator/consumer.py` con su
decisión registrada en el sitio; se mudan aquí cuando entre HU-AOS-11, que hoy edita ese
bloque.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.agents.responses import load_response
from core.kernel.decisions import bloquear
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Se agotó una cuota del período (mensajes o costo LLM). El texto real vive en
# core/playbooks/respuestas.md (`## quota_exhausted`); esto es el fallback
# determinístico si el playbook no está disponible (mismo patrón que FALLBACK_REPLY).
QUOTA_EXHAUSTED_REPLY = (
    "Por ahora no puedo seguir atendiendo esta conversación. "
    "Un asesor humano puede continuar la atención."
)


@dataclass(frozen=True)
class Bloqueo:
    motivo: str
    # Texto de cierre que se responde (None = el turno no responde nada).
    texto: str | None = None


async def conversacion_bloqueada(conversation: Any, customer: Any) -> bool:
    """Sin cliente, con el bot pausado o con la conversación escalada no corre el turno
    (la atiende una persona o el negocio la pausó)."""
    if customer is None:
        return True
    if conversation.status == "escalated":
        await bloquear(conversation, "escalated_conversation", status="silent")
        return True
    if conversation.bot_paused:
        await bloquear(conversation, "kill_switch", status="silent")
        return True
    return False


async def politicas_del_turno(conversation: Any, spec: Any) -> Bloqueo | None:
    """Cuota del período y presupuesto de la conversación, antes de gastar LLM."""
    # Cuota de turno (ADR-0045): si el período agotó `mensajes` o
    # `costo_llm_usd`, se corta el turno antes de gastar LLM. El bloqueo por
    # clientes/conversaciones/audio/OCR ocurre en el consumer/webhook (acción).
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        from core.billing.enforcement import turn_quota_metric

        blocked = await turn_quota_metric(tenant_id)
        if blocked is not None:
            log.warning(
                "turn_quota_exceeded",
                tenant_id=str(tenant_id),
                metric=blocked,
                conversation_id=str(conversation.id),
            )
            await bloquear(conversation, "quota")
            closure = await load_response(spec, "quota_exhausted") or QUOTA_EXHAUSTED_REPLY
            return Bloqueo("quota", closure)

    # Presupuesto agotado (ADR-0015): no se gasta más LLM en esta conversación
    # hasta que un admin suba el presupuesto o lo resetee.
    if conversation.state.get("budget_exhausted"):
        await bloquear(conversation, "budget", status="silent")
        return Bloqueo("budget")
    return None
