"""Historial por plan en las rutas del panel (HU-BILL-25, decisión del dueño 2026-11-22).

`historial_dias` del plan vigente **solo oculta** en el panel: nada se borra ni se archiva. Una
conversación se ve si tuvo actividad dentro de la ventana (empezó o recibió un mensaje desde el corte),
y de ella solo se muestran los mensajes desde el corte. Así una conversación larga (una por cliente y
canal) sigue en la bandeja si escribió ayer, aunque haya empezado hace un año.

Lo usan solo las rutas del panel del negocio (realm admin/gestor). NO lo usan el turno, la memoria
del agente, los crons ni las rutas master de plataforma.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, exists, or_, select

from core.api.plan_limits import plan_limit_error
from core.billing.features_del_plan import (
    FeatureNoIncluida,
    conversacion_fuera_del_historial,
    desde_para,
    historial_del_negocio,
)
from core.db.models import Conversation, Message


@dataclass(frozen=True)
class Historial:
    """El corte del panel para un negocio. `desde = None` = sin filtro."""

    plan_id: str | None
    dias: int | None
    desde: datetime | None

    def fuera(self) -> FeatureNoIncluida:
        return conversacion_fuera_del_historial(self.plan_id, self.dias)


SIN_FILTRO = Historial(plan_id=None, dias=None, desde=None)


async def historial_del_panel(tenant_id: uuid.UUID | None, *, session: Any = None) -> Historial:
    """El corte vigente del negocio (se recalcula en cada request: subir de plan se ve al instante)."""
    if tenant_id is None:
        return SIN_FILTRO
    plan_id, dias = await historial_del_negocio(tenant_id, session=session)
    return Historial(plan_id=plan_id, dias=dias, desde=desde_para(dias))


def conversacion_activa_desde(desde: datetime) -> ColumnElement[bool]:
    """La conversación empezó o tuvo un mensaje vivo desde `desde` (usa el índice
    `ix_messages_tenant_conv_created`). Correlacionada con `Conversation`."""
    return or_(
        Conversation.started_at >= desde,
        exists(
            select(Message.id).where(
                Message.tenant_id == Conversation.tenant_id,
                Message.conversation_id == Conversation.id,
                Message.deleted_at.is_(None),
                Message.created_at >= desde,
            )
        ),
    )


async def exigir_conversacion_visible(
    session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID, historial: Historial
) -> None:
    """409 `plan_feature_not_included` (`feature = historial`) si la conversación (que ya se sabe del
    negocio) no tiene actividad en la ventana del plan."""
    if historial.desde is None:
        return
    visible = (
        await session.execute(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
                conversacion_activa_desde(historial.desde),
            )
        )
    ).scalar_one_or_none()
    if visible is None:
        raise plan_limit_error(historial.fuera())


__all__ = [
    "SIN_FILTRO",
    "Historial",
    "conversacion_activa_desde",
    "exigir_conversacion_visible",
    "historial_del_panel",
]
