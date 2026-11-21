"""Asegurar el lead base del cliente en cada conversación (ADR-0205 §7 y §9; antes ADR-0193 §2, ❓3 de HU-AOS-70).

Reemplaza el `auto_create_lead` de commerce. Corre en el turno, justo después de fijar la release y el foco (que
se deciden juntos, bajo el lock de la conversación) y antes de los cortes por pausa, cuota o presupuesto: el
evento de conversación nueva llega antes de saber qué agente atiende y no se repite en una conversación ya abierta.
Desde ADR-0205 la fila base se crea en toda conversación real, tenga o no el foco Captar leads; enriquecerla sigue
siendo de Captar leads. Se salta la prueba del panel (su lead vive en el sandbox).

El aviso al dueño de un lead nuevo sale solo si el foco es el Coordinador (atiende sin especialista) o un agente con
Captar leads: un cliente que escribe a Cobranza obtiene su fila base sin aviso.

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
from core.shared.tenant import get_current_tenant_id
from core.shared.test_mode import is_panel_test

log = get_logger(__name__)

ACCION_QUE_LO_ACTIVA = "crear_lead"


def tiene_captar_leads(agente: Any) -> bool:
    """El agente en foco tiene Captar leads: su versión declara `crear_lead` (binding encendido)."""
    declaradas = declaradas_por(agente)
    return declaradas is not None and ACCION_QUE_LO_ACTIVA in declaradas


def avisa_lead_nuevo(agente: Any) -> bool:
    """El aviso de lead nuevo sale con el Coordinador en foco o con un agente que tiene Captar leads (ADR-0205 §9)."""
    if agente is None:
        return False
    return getattr(agente, "role", None) == "core" or tiene_captar_leads(agente)


async def asegurar_en_el_turno(agente: Any, customer: Any, conversation: Any) -> bool:
    """Asegura la fila base del lead en toda conversación real. True si la creó en este turno."""
    if customer is None or is_panel_test(conversation):
        return False
    if customer.tenant_id != get_current_tenant_id():  # el negocio sale del scope del turno, no del objeto
        log.warning("leads_asegurar_fuera_del_negocio")
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
            if creado and avisa_lead_nuevo(agente):
                from core.leads.tools import avisar_lead_nuevo

                await avisar_lead_nuevo(session, lead.id, lead.customer_id)
        return creado
    except Exception:
        log.warning("leads_asegurar_fallo", exc_info=True)
        return False
