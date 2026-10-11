"""Episodios de una conversación (ADR-0117 §4, ADR-0114 §1).

Un episodio es un tramo de conversación sin pausas mayores que `episode.idleMinutes`
(KernelSpec; 720 por defecto mientras el KernelSpec no exista, HU-AOS-12). En producción una
conversación no se cierra sola, así que el episodio es lo que acota la vida de una tarea y,
más adelante, la release asignada (HU-AOS-14): ambas cosas deben usar ESTA función, para que no
haya dos definiciones de episodio.

Al empezar un episodio nuevo:
1. las tareas abiertas sin actividad en todo el episodio anterior se cierran con
   `episode_expired` (antes que cualquier revalidación de release);
2. `conversations.episode_started_at` pasa al primer mensaje del episodio nuevo.

Se usa la hora de los mensajes, no la del reloj: el mismo historial da el mismo resultado
(también en el replay de las evals). La fila de la conversación se bloquea mientras se decide,
así dos turnos concurrentes no expiran dos veces; un reintento del mismo turno ya ve el
episodio nuevo y no hace nada (idempotente).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.kernel import state
from core.shared.logger import get_logger

log = get_logger(__name__)

DEFAULT_IDLE_MINUTES = 720


@dataclass(frozen=True)
class Episodio:
    inicio: datetime
    nuevo: bool                      # empezó en este turno (y se expiraron tareas viejas)
    anterior: datetime | None = None
    expiradas: tuple[uuid.UUID, ...] = ()


async def abrir_episodio(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    idle_minutes: int = DEFAULT_IDLE_MINUTES,
) -> Episodio | None:
    """Fija el episodio en curso al empezar el turno. None si no hay mensaje entrante pendiente.

    El turno empieza con el primer entrante posterior a la última saliente. Si el mensaje
    anterior a ese entrante es de hace más de `idle_minutes` (o no hay ninguno), ese entrante
    abre un episodio nuevo.
    """
    params = {"t": tenant_id, "c": conversation_id}
    guardado = (
        await session.execute(
            text(
                "SELECT episode_started_at FROM conversations "
                "WHERE tenant_id = :t AND id = :c FOR UPDATE"
            ),
            params,
        )
    ).first()
    if guardado is None:
        return None
    anterior: datetime | None = guardado[0]

    primer_entrante = (
        await session.execute(
            text(
                "SELECT min(m.created_at) FROM messages m "
                "WHERE m.tenant_id = :t AND m.conversation_id = :c AND m.deleted_at IS NULL "
                "AND m.direction = 'inbound' AND m.created_at > coalesce(("
                "  SELECT max(o.created_at) FROM messages o "
                "  WHERE o.tenant_id = :t AND o.conversation_id = :c AND o.deleted_at IS NULL "
                "  AND o.direction = 'outbound'), '-infinity'::timestamptz)"
            ),
            params,
        )
    ).scalar_one_or_none()
    if primer_entrante is None:
        return None
    previo = (
        await session.execute(
            text(
                "SELECT max(created_at) FROM messages "
                "WHERE tenant_id = :t AND conversation_id = :c AND deleted_at IS NULL "
                "AND created_at < :desde"
            ),
            {**params, "desde": primer_entrante},
        )
    ).scalar_one_or_none()

    corta = previo is None or primer_entrante - previo > timedelta(minutes=idle_minutes)
    if anterior is None:
        # Primer turno con episodios (bandera recién encendida): se fija sin expirar nada.
        await _fijar(session, params, primer_entrante)
        return Episodio(inicio=primer_entrante, nuevo=False)
    if not corta or primer_entrante <= anterior:
        return Episodio(inicio=anterior, nuevo=False)

    expiradas = await state.expirar_tareas_inactivas(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        sin_actividad_desde=anterior,
        now=primer_entrante,
    )
    await _fijar(session, params, primer_entrante)
    log.info(
        "episode_started",
        conversation_id=str(conversation_id),
        expired_tasks=len(expiradas),
    )
    return Episodio(inicio=primer_entrante, nuevo=True, anterior=anterior, expiradas=tuple(expiradas))


async def _fijar(session: AsyncSession, params: dict[str, uuid.UUID], inicio: datetime) -> None:
    await session.execute(
        text("UPDATE conversations SET episode_started_at = :inicio WHERE tenant_id = :t AND id = :c"),
        {**params, "inicio": inicio},
    )
