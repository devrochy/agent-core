"""Barrido de corridas de pruebas colgadas (HU-AOS-103, ADR-0212 §2 y §5).

Un worker que muere o se reinicia a mitad de una corrida deja dos cosas: la corrida `corriendo` (o `en_cola` si nunca
se encoló) y, a lo más, la conversación del intento en curso. Sin este barrido la corrida bloquearía al negocio para
siempre (`uq_agent_test_runs_activa`: una activa por negocio) y el gasto de la conversación nunca se cobraría.

- Una corrida activa con más de `HORAS_COLGADA` (2 h) desde que arrancó (o se creó, si nunca arrancó) pasa a `fallo`
  con motivo `colgada`. El `UPDATE` solo toca estados activos: no pisa una que terminó justo ahora.
- Toda conversación con `state.prueba_corrida` de más de `HORAS_CONVERSACION` (1 h) se consolida y se purga por el
  MISMO camino del corredor (`consolidar_y_purgar`: una transacción, así que no se cobra dos veces). `playground_cleanup`
  las excluye a propósito (no puede purgarlas sin perder el cobro): este es su único limpiador. El costo del cliente
  simulado de ese intento no se puede recuperar (vive en memoria del worker caído); el de los turnos del agente sí.

Corre por negocio dentro de su `tenant_scope`, como `playground_cleanup`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from core.db.models import AgentTestRun, Conversation, Tenant
from core.db.session import get_session
from core.pruebas import corredor
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

HORAS_COLGADA = 2
HORAS_CONVERSACION = 1


async def barrer_negocio(tenant_id: uuid.UUID, *, ahora: datetime | None = None) -> dict[str, int]:
    """Cierra las corridas colgadas del negocio y purga sus conversaciones huérfanas. Dentro del `tenant_scope`."""
    ahora = ahora or datetime.now(UTC)
    corte_corrida = ahora - timedelta(hours=HORAS_COLGADA)
    corte_conversacion = ahora - timedelta(hours=HORAS_CONVERSACION)
    async with get_session() as session:
        # Lo que arrancó cuenta desde ahí; lo que nunca arrancó (se perdió el encolado), desde que se creó.
        colgadas = list(
            (
                await session.execute(
                    select(AgentTestRun.id).where(
                        AgentTestRun.tenant_id == tenant_id,
                        AgentTestRun.estado.in_(corredor.ACTIVAS),
                        AgentTestRun.deleted_at.is_(None),
                        func.coalesce(AgentTestRun.iniciada_en, AgentTestRun.created_at) < corte_corrida,
                    )
                )
            ).scalars()
        )
        huerfanas = [
            (c, estado["prueba_corrida"])
            for c, estado in (
                await session.execute(
                    select(Conversation.id, Conversation.state).where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.channel == TEST_CHANNEL,
                        Conversation.state["prueba_corrida"].astext.is_not(None),
                        Conversation.started_at < corte_conversacion,
                    )
                )
            ).all()
        ]
    cerradas = 0
    for run_id in colgadas:
        if await corredor.cerrar(tenant_id, run_id, corredor.FALLO, codigo="colgada"):
            cerradas += 1
    purgadas = 0
    for conversation_id, run in huerfanas:
        try:
            run_id = uuid.UUID(str(run))
        except ValueError:
            continue  # un `state` ilegible: dato, no orden; se deja y queda en el log
        try:
            creditos, _ = await corredor.consolidar_y_purgar(tenant_id, conversation_id, run_id=run_id)
        except Exception:
            log.exception("prueba_barrido_purga_fallo", tenant_id=str(tenant_id), conversation_id=str(conversation_id))
            continue
        purgadas += 1
        if creditos > 0:
            await corredor._sumar_gasto(tenant_id, run_id, Decimal(creditos))
    return {"corridas": cerradas, "conversaciones": purgadas}


async def barrer_corridas_colgadas() -> dict[str, int]:
    async with get_session() as session:
        negocios = list((await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))).scalars())
    total = {"corridas": 0, "conversaciones": 0, "tenants": 0}
    for tenant_id in negocios:
        try:
            async with tenant_scope(tenant_id):
                hecho = await barrer_negocio(tenant_id)
        except Exception:
            log.exception("prueba_barrido_negocio_fallo", tenant_id=str(tenant_id))
            continue
        if hecho["corridas"] or hecho["conversaciones"]:
            total["tenants"] += 1
        total["corridas"] += hecho["corridas"]
        total["conversaciones"] += hecho["conversaciones"]
    if total["corridas"] or total["conversaciones"]:
        log.info("prueba_barrido", **total)
    return total


__all__ = ["HORAS_COLGADA", "HORAS_CONVERSACION", "barrer_corridas_colgadas", "barrer_negocio"]
