"""Vencimiento de las tareas que esperan su evento de cierre (HU-AOS-07, cierre por pago).

`tomar_pedido` con pago en línea queda en `esperar_pago` hasta que llega
`order.paid` (o el cliente dice que pagó y `verificar_pago_pedido` lo confirma). Si el pago no
llega y el cliente no vuelve a escribir (el episodio siguiente ya cancela lo inactivo, ADR-0114),
la tarea no queda abierta para siempre: este cron cancela con motivo `espera_vencida` las tareas
abiertas de un procedimiento con `completion.events` que siguen en su ÚLTIMA etapa sin actividad
hace más de `ESPERA_MAXIMA` (el link vence a las 24 h por defecto, `link_expira_horas`).

- No le escribe al cliente ni escala: es limpieza. El pedido sigue vivo y, si después se paga,
  commerce lo marca pagado igual. No es el `recovery.timeout` del contrato (sigue pendiente).
- Solo recorre los negocios con `tenants.aos_procedures` (con las banderas apagadas no consulta
  ninguna tarea), cada uno bajo su `tenant_scope`.
- Una transacción por tarea y orden de bloqueo conversación → tarea, como el turno. Si un turno
  tiene la conversación bloqueada (`SKIP LOCKED`), la tarea queda para la corrida siguiente: el
  cron nunca espera a una conversación ni la frena.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, text

from core.db.models import ConversationTask
from core.kernel import state
from core.kernel.procedures import registro
from core.shared.logger import get_logger

log = get_logger(__name__)

ESPERA_MAXIMA = timedelta(hours=72)
MOTIVO = "espera_vencida"
_LOTE = 200


def _esperando(fila: ConversationTask) -> bool:
    """La tarea está en la última etapa de un procedimiento que se cierra por evento."""
    proc = registro.definicion(fila.procedure_id, fila.procedure_version)
    return bool(proc is not None and proc.completion and proc.completion.events and fila.stage == proc.stages[-1].id)


async def vencer_esperas_del_negocio(tenant_id: uuid.UUID, *, now: datetime | None = None) -> int:
    """Cancela las esperas vencidas de un negocio. Devuelve cuántas cerró."""
    from core.db.session import get_session
    from core.kernel.procedures.ejecucion import _devolver_control
    from core.shared.tenant import tenant_scope

    # La última etapa de cada versión, en SQL: tareas abandonadas en etapas anteriores (que nadie
    # cierra si el cliente no vuelve) no pueden llenar el lote y tapar las que esperan el pago.
    en_espera = [
        and_(
            ConversationTask.procedure_id == p.id,
            ConversationTask.procedure_version == p.version,
            ConversationTask.stage == p.stages[-1].id,
        )
        for p in registro.con_cierre_por_evento()
    ]
    if not en_espera:
        return 0
    limite = (now or datetime.now(UTC)) - ESPERA_MAXIMA
    cerradas = 0
    async with tenant_scope(tenant_id):
        async with get_session() as s:
            filas = (
                await s.execute(
                    select(ConversationTask)
                    .where(
                        ConversationTask.tenant_id == tenant_id,
                        ConversationTask.status.in_(state.ABIERTOS),
                        or_(*en_espera),
                        ConversationTask.updated_at < limite,
                    )
                    .order_by(ConversationTask.updated_at)
                    .limit(_LOTE)
                )
            ).scalars().all()
            candidatas = [(f.id, f.conversation_id) for f in filas if _esperando(f)]
        for task_id, conversation_id in candidatas:
            try:
                async with get_session() as s:
                    libre = (
                        await s.execute(
                            text(
                                "SELECT 1 FROM conversations WHERE tenant_id = :t AND id = :c "
                                "FOR UPDATE SKIP LOCKED"
                            ),
                            {"t": tenant_id, "c": conversation_id},
                        )
                    ).first()
                    if libre is None:
                        continue  # un turno la tiene: queda para la corrida siguiente
                    fila = (
                        await s.execute(
                            select(ConversationTask).where(
                                ConversationTask.tenant_id == tenant_id, ConversationTask.id == task_id
                            )
                        )
                    ).scalar_one_or_none()
                    if fila is None:
                        continue
                    await s.refresh(fila)
                    if fila.status not in state.ABIERTOS or fila.updated_at >= limite or not _esperando(fila):
                        continue  # se movió desde la lectura
                    proc = registro.definicion(fila.procedure_id, fila.procedure_version)
                    fila = await state.registrar(
                        s, tenant_id=tenant_id, conversation_id=conversation_id, task_id=task_id,
                        tipo="cancelled", data={"reason": MOTIVO}, actor="kernel",
                    )
                    await _devolver_control(s, fila, proc)
                    cerradas += 1
            except Exception as exc:
                # Sin traza: el error de SQLAlchemy repite parámetros (datos de la tarea).
                log.error("motor_espera_vencida_fallo", task_id=str(task_id), error_type=type(exc).__name__)
    if cerradas:
        log.info("motor_esperas_vencidas", tenant_id=str(tenant_id), count=cerradas)
    return cerradas


async def vencer_esperas(*, now: datetime | None = None) -> int:
    """Cron: recorre los negocios con el motor de procedimientos encendido."""
    from core.db.session import get_session
    from core.kernel.banderas import negocios_con_procedimientos

    if not registro.con_cierre_por_evento():
        return 0
    async with get_session() as s:
        negocios = await negocios_con_procedimientos(s)
    total = 0
    for tenant_id in negocios:
        try:
            total += await vencer_esperas_del_negocio(tenant_id, now=now)
        except Exception as exc:
            log.error("motor_esperas_negocio_fallo", tenant_id=str(tenant_id), error_type=type(exc).__name__)
    return total
