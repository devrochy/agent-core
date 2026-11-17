"""Asegurar el lead del cliente en cada conversación (ADR-0193 §2, ❓3 de HU-AOS-70).

Reemplaza el `auto_create_lead` de commerce. Corre en el turno, DESPUÉS del ruteo: el evento de conversación nueva
llega antes de saber qué agente atiende y no se repite en una conversación ya abierta. Solo si el agente en foco tiene
Captar leads (declara `crear_lead` en su versión). Se salta la prueba del panel (su lead vive en el sandbox).

Idempotente por cliente: un turno que repite, dos turnos a la vez o una conversación reabierta dejan el mismo lead.
Nunca rompe el turno: un fallo queda en el log y el turno sigue.
"""

from __future__ import annotations

from typing import Any

from core.db.session import get_session
from core.kernel.oferta import declaradas_por
from core.leads import servicio
from core.leads.catalogo import origen_del_canal
from core.shared.logger import get_logger
from core.shared.test_mode import is_panel_test

log = get_logger(__name__)

ACCION_QUE_LO_ACTIVA = "crear_lead"


def tiene_captar_leads(agente: Any) -> bool:
    """El agente en foco tiene Captar leads: su versión declara `crear_lead` (binding encendido)."""
    declaradas = declaradas_por(agente)
    return declaradas is not None and ACCION_QUE_LO_ACTIVA in declaradas


async def asegurar_en_el_turno(agente: Any, customer: Any, conversation: Any) -> bool:
    """Asegura el lead si corresponde. True si lo creó en este turno."""
    if agente is None or customer is None or is_panel_test(conversation):
        return False
    if not tiene_captar_leads(agente):
        return False
    try:
        async with get_session() as session:
            lead, creado = await servicio.asegurar(
                session,
                customer.tenant_id,
                customer.id,
                origen=origen_del_canal(getattr(conversation, "channel", None)),
                conversation_id=getattr(conversation, "id", None),
            )
            if creado:
                from core.leads.tools import avisar_lead_nuevo

                await avisar_lead_nuevo(session, lead.id, lead.customer_id)
        return creado
    except Exception:
        log.warning("leads_asegurar_fallo", exc_info=True)
        return False
