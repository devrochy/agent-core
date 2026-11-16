"""Interruptor global de la plataforma (HU-ARCH-38, ADR-0200): estado, cambios e historial.

Único punto que escribe `platform_kill_switch` y `platform_kill_switch_events` (tablas de SISTEMA, sin RLS;
`tests/test_kill_switch_tablas_solo_desde_su_servicio.py` lo vigila). Lo usan la ruta master-only
(`core/api/routers/kill_switch.py`) y la CLI (`agent-core admin kill-switch`). Quien llama a `fijar` ya
verificó el rol master; un intento de alguien que no lo es se registra con `registrar_denegado`.

El camino caliente no lee desde aquí: lo hace `core/shared/kill_switch.py`, con caché y fallo cerrado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update

from core.db.models import PlatformKillSwitch, PlatformKillSwitchEvent, utcnow
from core.db.session import get_session
from core.shared import kill_switch
from core.shared.logger import get_logger

log = get_logger(__name__)

MAX_MOTIVO = 300
FUENTES = frozenset({"api", "cli"})
HISTORIAL = 20


class MotivoRequerido(ValueError):
    """Activar el interruptor exige decir por qué."""


@dataclass(frozen=True)
class Evento:
    action: str
    reason: str | None
    actor_label: str | None
    source: str
    created_at: datetime


@dataclass(frozen=True)
class Estado:
    active: bool
    reason: str | None
    changed_by_label: str | None
    source: str | None
    changed_at: datetime | None
    history: list[Evento]


def _motivo(motivo: str | None) -> str | None:
    texto = " ".join((motivo or "").split())[:MAX_MOTIVO]
    return texto or None


async def estado() -> Estado:
    async with get_session() as session:
        fila = await session.get(PlatformKillSwitch, 1)
        eventos = (
            await session.execute(
                select(PlatformKillSwitchEvent).order_by(PlatformKillSwitchEvent.created_at.desc()).limit(HISTORIAL)
            )
        ).scalars().all()
    historial = [Evento(e.action, e.reason, e.actor_label, e.source, e.created_at) for e in eventos]
    if fila is None:
        return Estado(False, None, None, None, None, historial)
    return Estado(fila.active, fila.reason, fila.changed_by_label, fila.source, fila.changed_at, historial)


async def fijar(
    activo: bool, motivo: str | None, *, admin_id: uuid.UUID | None, etiqueta: str, fuente: str
) -> Estado:
    """Activa o desactiva el interruptor global (estado + evento en la misma transacción) y avisa a Slack."""
    if fuente not in FUENTES:
        raise ValueError(f"fuente inválida: {fuente!r}")
    razon = _motivo(motivo)
    if activo and razon is None:
        raise MotivoRequerido("Para activar el interruptor global hay que decir por qué.")
    accion = "activated" if activo else "deactivated"
    async with get_session() as session:
        await session.execute(
            update(PlatformKillSwitch)
            .where(PlatformKillSwitch.id == 1)
            .values(
                active=activo, reason=razon, changed_by=admin_id, changed_by_label=etiqueta,
                source=fuente, changed_at=utcnow(),
            )
        )
        session.add(
            PlatformKillSwitchEvent(
                action=accion, reason=razon, actor_admin_id=admin_id, actor_label=etiqueta, source=fuente
            )
        )
    kill_switch.invalidar()
    log.warning("kill_switch_cambio", nivel="global", accion=accion, fuente=fuente, actor=etiqueta)
    _metrica("global", accion)
    await _avisar(activo, razon, etiqueta)
    return await estado()


async def registrar_denegado(*, admin_id: uuid.UUID | None, etiqueta: str, fuente: str = "api") -> None:
    """Un intento de cambiar el interruptor sin ser master: queda en el historial (no cambia nada)."""
    log.warning("kill_switch_acceso_denegado", actor=etiqueta, fuente=fuente)
    try:
        async with get_session() as session:
            session.add(
                PlatformKillSwitchEvent(action="denied", actor_admin_id=admin_id, actor_label=etiqueta, source=fuente)
            )
    except Exception:  # el 403 se responde igual aunque no se pueda registrar
        log.exception("kill_switch_denegado_no_registrado")
    _metrica("global", "denied")


async def ultima_reactivacion() -> datetime | None:
    """Cuándo se desactivó por última vez (None si nunca): la recuperación de pendientes no responde en lote lo
    que llegó antes (ADR-0200)."""
    async with get_session() as session:
        return (
            await session.execute(
                select(PlatformKillSwitchEvent.created_at)
                .where(PlatformKillSwitchEvent.action == "deactivated")
                .order_by(PlatformKillSwitchEvent.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def _metrica(nivel: str, accion: str) -> None:
    from core.observability.metricas_negocio import kill_switch_cambio

    kill_switch_cambio(nivel, accion)


async def _avisar(activo: bool, motivo: str | None, actor: str) -> None:
    from core.shared.alerts import send_alert

    try:
        if activo:
            await send_alert(
                "Interruptor global ACTIVADO",
                f"Se detuvieron todos los agentes y los envíos automáticos. Quién: {actor}. Motivo: {motivo}",
                severity="critical",
            )
        else:
            await send_alert("Interruptor global desactivado", f"Los agentes vuelven a responder. Quién: {actor}.", severity="info")
    except Exception:  # Slack caído no deshace el cambio
        log.exception("kill_switch_alerta_fallida")
