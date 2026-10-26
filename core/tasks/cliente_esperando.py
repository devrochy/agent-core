"""Aviso «cliente esperando respuesta» (ADR-0167, categoría conversaciones).

Cada pocos minutos revisa, negocio por negocio, las conversaciones con una escalación abierta
(`pendiente` o `asignada`, el agente está pausado) y avisa cuando un cliente lleva más de
`UMBRAL_MINUTOS` sin respuesta humana. El reloj del episodio arranca en el primer mensaje del
cliente posterior a la última respuesta de una persona del equipo; si nadie ha respondido desde
que se escaló, arranca con la escalación. Un episodio avisa una sola vez (clave de deduplicación
con su inicio): si el equipo responde y el cliente vuelve a esperar, es otro episodio.

Dos topes protegen de ráfagas: no avisa de episodios de más de `VENTANA_MAXIMA_HORAS` (una
escalación olvidada de hace días no genera un aviso al desplegar) y mira como máximo `LOTE`
escalaciones por negocio y pasada. Si la escalación está asignada, el aviso va dirigido a esa
persona (le llega aunque no tenga acceso a Escalaciones).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from core.db.models import Escalation, Message, Tenant
from core.db.session import get_session
from core.notifications import catalog
from core.notifications.service import notify_standalone
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

UMBRAL_MINUTOS = 15
VENTANA_MAXIMA_HORAS = 24
LOTE = 200


async def _inicio_de_espera(session, esc: Escalation) -> datetime | None:
    """Desde cuándo espera el cliente de esta escalación, o `None` si no está esperando."""
    ultima_humana = (
        await session.execute(
            select(func.max(Message.created_at)).where(
                Message.tenant_id == esc.tenant_id,
                Message.conversation_id == esc.conversation_id,
                Message.direction == "outbound",
                Message.sender_type == "human",
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if ultima_humana is None or ultima_humana < esc.created_at:
        # Nadie del equipo ha respondido desde que se escaló.
        return esc.created_at
    # Una persona respondió: espera solo si el cliente escribió después.
    return (
        await session.execute(
            select(func.min(Message.created_at)).where(
                Message.tenant_id == esc.tenant_id,
                Message.conversation_id == esc.conversation_id,
                Message.direction == "inbound",
                Message.created_at > ultima_humana,
                Message.deleted_at.is_(None),
            )
        )
    ).scalar_one()


async def avisar_clientes_esperando(
    ahora: datetime | None = None, umbral_minutos: int = UMBRAL_MINUTOS
) -> dict[str, int]:
    """Emite un aviso por cada episodio de espera vencido. Devuelve conteos."""
    ahora = ahora or datetime.now(UTC)
    limite = ahora - timedelta(minutes=umbral_minutos)
    piso = ahora - timedelta(hours=VENTANA_MAXIMA_HORAS)
    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()

    avisos = 0
    for tenant_id in tenant_ids:
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    abiertas = (
                        await session.execute(
                            select(Escalation)
                            .where(
                                Escalation.tenant_id == tenant_id,
                                Escalation.status != "resuelta",
                                Escalation.deleted_at.is_(None),
                            )
                            .order_by(Escalation.created_at.desc())
                            .limit(LOTE)
                        )
                    ).scalars().all()
                    candidatas = []
                    for esc in abiertas:
                        desde = await _inicio_de_espera(session, esc)
                        if desde is not None and piso <= desde <= limite:
                            candidatas.append((esc, desde))
            for esc, desde in candidatas:
                nid = await notify_standalone(
                    tenant_id,
                    catalog.cliente_esperando(
                        esc.id,
                        esc.conversation_id,
                        desde=desde,
                        umbral_minutos=umbral_minutos,
                        asignada_a=esc.assigned_admin_id,
                    ),
                )
                avisos += int(nid is not None)
        except Exception as exc:  # un negocio con problemas no frena a los demás
            log.warning("cliente_esperando_fallo", tenant_id=str(tenant_id), error=type(exc).__name__)

    if avisos:
        log.info("cliente_esperando", avisos=avisos)
    return {"avisos": avisos}
