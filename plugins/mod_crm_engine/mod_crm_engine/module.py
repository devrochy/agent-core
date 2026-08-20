"""CRMModule: el BaseModule del plugin.

Demuestra el contrato completo:
- Dos skills (LeadSkill + DealSkill).
- Suscripción a eventos del core (auto-crear lead al iniciar conversación).
- Guardrail propio (los montos mencionados deben venir de tools).
- Menú del panel admin (desaparece al desactivar).
- Cron job vía jobs.py (importado al activar).
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from core.mcp.types import BaseModule, GuardrailResult
from core.shared.events import ON_CONVERSATION_STARTED
from core.shared.logger import get_logger
from mod_crm_engine.config import CRMConfig
from mod_crm_engine.skills import DealSkill, LeadSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

MONEY_IN_TEXT = re.compile(r"\$\s?[\d.,]+")


class CRMModule(BaseModule):
    id = "mod_crm_engine"
    name = "CRM Engine"
    version = "1.0.0"
    description = "Leads, deals y seguimiento automático de prospectos"
    requires_config = True
    config_schema = CRMConfig
    admin_menu = [
        {"label": "CRM · Leads", "path": "/admin/plugins/mod_crm_engine/leads"},
    ]
    # para que el CLI documente las skills en el SDD sin activar el plugin
    declared_skills = [LeadSkill, DealSkill]

    def on_activate(self, host: "MCPHost") -> None:
        host.register_skill(LeadSkill())
        host.register_skill(DealSkill())
        host.register_guardrail("crm_amount_check", self._amount_guardrail, priority=5)

        config = self.config
        if config is not None and getattr(config, "auto_create_lead", True):
            host.subscribe(ON_CONVERSATION_STARTED, self._on_conversation_started)

        # importar jobs.py registra los @register_cron en Celery Beat
        import mod_crm_engine.jobs  # noqa: F401

    # ------------------------------------------------------------ eventos
    async def _on_conversation_started(self, conversation_id: uuid.UUID, customer_id: uuid.UUID) -> None:
        """Crea un lead borrador cuando arranca una conversación nueva."""
        from sqlalchemy import select

        from core.db.session import get_session
        from mod_crm_engine.models import Lead

        async with get_session() as session:
            existing = (
                await session.execute(select(Lead).where(Lead.customer_id == customer_id))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Lead(
                        customer_id=customer_id,
                        conversation_id=conversation_id,
                        status="nuevo",
                        idempotency_key=f"auto:{conversation_id}",
                    )
                )
                log.info("lead_draft_created", conversation_id=str(conversation_id))

    # ------------------------------------------------------------ guardrail
    async def _amount_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Si la respuesta menciona montos ($) que no salieron de una tool, bloquear.

        Mismo principio que el price guardrail de agent-sale: el LLM no inventa cifras.
        """
        amounts_in_text = MONEY_IN_TEXT.findall(response_text)
        if not amounts_in_text:
            return GuardrailResult(passed=True)

        known: set[str] = set()
        for result in tool_results:
            for value in result.values():
                if isinstance(value, (int, float)):
                    known.add(f"{value:g}")

        def normalize(amount: str) -> str:
            return amount.replace("$", "").replace(".", "").replace(",", "").strip()

        for amount in amounts_in_text:
            cleaned = normalize(amount)
            if cleaned and not any(cleaned == normalize(k) or cleaned in k for k in known):
                return GuardrailResult(
                    passed=False,
                    reason=f"Monto no verificado en la respuesta: {amount}",
                    retry_instruction=(
                        "Mencionaste un monto que no proviene de ninguna herramienta. "
                        "No cites cifras específicas salvo que una herramienta las haya devuelto."
                    ),
                )
        return GuardrailResult(passed=True)
