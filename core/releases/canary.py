"""Canary de una conexión: probar una versión con una parte de las conversaciones nuevas (HU-AOS-32,
adenda de ADR-0117 que enmienda §5).

- **Iniciar** (como publicar: gate aprobado si está cerrado, `aos_runtime = v3`, un canary por
  conexión): fija `canary_release_id` / `canary_percent` y deja `canary_started` con
  `{percent, base_release_id, umbrales, gate, gate_tocados, gate_motivo}`. El reparto sticky lo hace
  `core.kernel.release_del_turno` al empezar cada episodio.
- **Cambiar el porcentaje** (1-50, auditado) y **detener** (`canary_stopped` con su motivo; el dueño
  detiene con `detenida_por_dueno`, que no bloquea la versión).
- **Publicar para todos:** el veredicto se decide una sola vez (al juntar la muestra se guarda en
  `canary_evaluated`); exige `listo`, o 7 días sin muestra, y que no haya señal dura activa.
  Promueve con el gate guardado al iniciar y limpia el puntero, en la misma transacción.
- **Estado:** muestra, comparación por carril, veredicto y señal dura (solo lectura).

Todo bajo el `FOR UPDATE` de la conexión (`promocion._bloquear`), dentro del `tenant_scope` del
negocio y filtrando por `tenant_id`. La reversión automática y los avisos son la parte 2: hasta
entonces `iniciar` responde 409 `canary_no_disponible` (`CANARY_DISPONIBLE`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.releases import canary_evaluacion as ev
from core.releases.build import registrar_evento
from core.releases.promocion import (
    DETENIDA_POR_DUENO,
    ConflictoDeRelease,
    _bloquear,
    _exigir_utilizable,
    _release,
    detener_canary,
    gate_aprobado,
    promover_en,
)
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Parte 1 sin el job: un canary vivo no se revertiría solo (contradice la enmienda de §5).
CANARY_DISPONIBLE = False
PORCENTAJE_MIN, PORCENTAJE_MAX = 1, 50


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("canary: fuera del tenant_scope del negocio")


def _porcentaje(percent: int) -> int:
    if not PORCENTAJE_MIN <= int(percent) <= PORCENTAJE_MAX:
        raise ConflictoDeRelease(
            "porcentaje_invalido", f"El porcentaje va de {PORCENTAJE_MIN} a {PORCENTAJE_MAX} %."
        )
    return int(percent)


async def _exigir_v3(session: Any, tenant_id: uuid.UUID) -> None:
    runtime = (
        # FOR SHARE: no se cruza con un `migrar-a-v3 --volver` que escribe la bandera en paralelo.
        await session.execute(text("SELECT aos_runtime FROM tenants WHERE id = :t FOR SHARE"), {"t": tenant_id})
    ).scalar_one_or_none()
    if runtime != "v3":
        raise ConflictoDeRelease(
            "requiere_v3", "La prueba con una parte de los clientes exige el Agent OS (aos_runtime = v3)."
        )


async def _sin_canary(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> tuple[Any, Any]:
    activa, canary = await _bloquear(session, tenant_id, connection_id)
    if canary is None:
        raise ConflictoDeRelease("sin_canary", "No hay ninguna versión en prueba en esta conexión.")
    return activa, canary


# ------------------------------------------------------------------ iniciar, porcentaje, detener
async def iniciar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    percent: int,
    actor: str,
    forzado_por_master: str | None = None,
) -> None:
    """Empieza a probar `release_id` con `percent` % de las conversaciones nuevas."""
    _exigir_scope(tenant_id)
    if not CANARY_DISPONIBLE:
        raise ConflictoDeRelease(
            "canary_no_disponible", "La prueba con una parte de los clientes todavía no está disponible."
        )
    percent = _porcentaje(percent)
    forzado = bool(forzado_por_master and forzado_por_master.strip())
    async with get_session() as session:
        await _exigir_v3(session, tenant_id)
        activa, canary = await _bloquear(session, tenant_id, connection_id)
        await _release(session, tenant_id, connection_id, release_id)
        if activa is None:
            raise ConflictoDeRelease("sin_release_activa", "La conexión no tiene una versión activa.")
        if canary is not None:
            raise ConflictoDeRelease("canary_en_curso", "Ya hay una versión en prueba en esta conexión.")
        if release_id == activa:
            raise ConflictoDeRelease("ya_activa", "Esa versión ya es la activa.")
        await _exigir_utilizable(session, tenant_id, release_id)
        aprobado, gate, tocados = await gate_aprobado(session, tenant_id, release_id, activa, forzado=forzado)
        if not aprobado and gate == "pendiente":
            raise ConflictoDeRelease(
                "gate_pendiente",
                "La versión todavía no se ha medido contra la activa. La medición está en camino; "
                "vuelve a intentarlo cuando termine.",
                {"estado": "sin_medicion", "base_release_id": str(activa), "tocados": len(tocados)},
            )
        if not aprobado:
            raise ConflictoDeRelease("gate_rechazado", "La versión no pasó el gate de pass^k.")
        await session.execute(
            text(
                "UPDATE channel_connections SET canary_release_id = :r, canary_percent = :p "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant_id, "c": connection_id, "r": release_id, "p": percent},
        )
        datos: dict[str, Any] = {
            "percent": percent,
            "base_release_id": str(activa),
            "umbrales": dict(ev.UMBRALES),
            "gate": gate,
            "gate_tocados": dict(sorted(tocados.items())[:50]),
        }
        if forzado:
            datos["gate_motivo"] = str(forzado_por_master).strip()[:200]
        await registrar_evento(session, tenant_id, connection_id, release_id, "canary_started", actor, datos)
        if forzado:
            from core.db.models import AuditLog

            session.add(
                AuditLog(
                    tenant_id=tenant_id, actor=actor[:120], action="release_gate_forzado",
                    input={"connection_id": str(connection_id), "release_id": str(release_id), "motivo": datos["gate_motivo"]},
                )
            )
    log.info("release_canary_iniciado", connection_id=str(connection_id), percent=percent, gate=gate)


async def cambiar_porcentaje(tenant_id: uuid.UUID, connection_id: uuid.UUID, *, percent: int, actor: str) -> None:
    """Cambia el porcentaje. No reasigna a mitad de episodio: rige al empezar el siguiente."""
    _exigir_scope(tenant_id)
    percent = _porcentaje(percent)
    from core.db.models import AuditLog

    async with get_session() as session:
        _activa, canary = await _sin_canary(session, tenant_id, connection_id)
        await session.execute(
            text("UPDATE channel_connections SET canary_percent = :p WHERE tenant_id = :t AND id = :c"),
            {"t": tenant_id, "c": connection_id, "p": percent},
        )
        session.add(
            AuditLog(
                tenant_id=tenant_id, actor=actor[:120], action="release_canary_porcentaje",
                input={"connection_id": str(connection_id), "release_id": str(canary), "percent": percent},
            )
        )


async def detener(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, *, actor: str, motivo: str = DETENIDA_POR_DUENO
) -> None:
    """Detiene el canary. Con el motivo del dueño la versión se puede volver a probar."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        _activa, canary = await _sin_canary(session, tenant_id, connection_id)
        await detener_canary(session, tenant_id, connection_id, canary, motivo=motivo, actor=actor)


async def detener_todos(tenant_id: uuid.UUID, *, actor: str, motivo: str = "reemplazado") -> int:
    """Detiene los canaries de todas las conexiones del negocio (al volver a legacy). Devuelve cuántos."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT id, canary_release_id FROM channel_connections WHERE tenant_id = :t "
                    "AND canary_release_id IS NOT NULL AND deleted_at IS NULL ORDER BY id FOR UPDATE"
                ),
                {"t": tenant_id},
            )
        ).all()
        for cid, canary in filas:
            await detener_canary(session, tenant_id, cid, canary, motivo=motivo, actor=actor)
    return len(filas)


# ------------------------------------------------------------------ evaluación y publicar
@dataclass
class Estado:
    release_id: uuid.UUID
    base_release_id: uuid.UUID | None
    percent: int
    desde: datetime
    horas: float
    veredicto: ev.Veredicto
    senal_dura: ev.SenalDura
    guardado: bool = False
    puede_publicar: bool = False
    motivo_no_publicar: str | None = None
    estable: ev.Carril = field(default_factory=ev.Carril)
    canary: ev.Carril = field(default_factory=ev.Carril)
    umbrales: dict[str, Any] = field(default_factory=dict)
    release_number: int | None = None
    base_release_number: int | None = None


async def _inicio(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID) -> tuple[datetime, dict[str, Any]]:
    fila = (
        await session.execute(
            text(
                "SELECT created_at, data FROM release_events WHERE tenant_id = :t AND release_id = :r "
                "AND type = 'canary_started' ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id},
        )
    ).first()
    if fila is None:
        raise ConflictoDeRelease("sin_canary", "No hay ninguna versión en prueba en esta conexión.")
    return fila[0], dict(fila[1] or {})


async def _guardado(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID, desde: datetime) -> dict[str, Any] | None:
    """El veredicto ya decidido de este canary (`canary_evaluated` posterior a su inicio)."""
    fila = (
        await session.execute(
            text(
                "SELECT data FROM release_events WHERE tenant_id = :t AND release_id = :r "
                "AND type = 'canary_evaluated' AND created_at >= :desde ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id, "desde": desde},
        )
    ).scalar_one_or_none()
    return dict(fila) if isinstance(fila, dict) else None


async def _carril(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID | None, lane: str,
    desde: datetime, hasta: datetime,
) -> ev.Carril:
    from core.metricas import fiabilidad as m

    if release_id is None:
        return ev.Carril()
    f = m.Filtro(desde=desde, hasta=hasta, connection_id=connection_id, release_id=release_id, lane=lane)
    op = await m.operativas(session, tenant_id, f)
    terminadas, completadas = await m.completitud_total(session, tenant_id, f)
    return ev.Carril(
        conversaciones=op.conversaciones, conversaciones_escaladas=op.conversaciones_escaladas,
        tareas_terminadas=terminadas, tareas_completadas=completadas, costo_usd=op.costo_usd, p95_ms=op.p95_ms,
    )


async def evaluar(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, canary: uuid.UUID, ahora: datetime) -> Estado:
    """Calcula el estado del canary (sin escribir). Usa el veredicto guardado si ya se decidió."""
    from core.metricas import fiabilidad as m

    _exigir_scope(tenant_id)

    desde, datos = await _inicio(session, tenant_id, canary)
    u = ev.umbrales(datos.get("umbrales"))
    base = uuid.UUID(str(datos["base_release_id"])) if datos.get("base_release_id") else None
    estable = await _carril(session, tenant_id, connection_id, base, "stable", desde, ahora)
    candidato = await _carril(session, tenant_id, connection_id, canary, "canary", desde, ahora)
    horas = max(0.0, (ahora - desde).total_seconds() / 3600)
    guardado = await _guardado(session, tenant_id, canary, desde)
    if guardado is not None:
        veredicto = ev.Veredicto(
            str(guardado.get("veredicto")), list(guardado.get("motivos") or []),
            list(guardado.get("advertencias") or []), dict(guardado.get("detalle") or {}),
        )
    else:
        veredicto = ev.decidir(candidato, estable, horas, u)
    ventana = int(u["dura_ventana"])
    recientes = await m.conversaciones_recientes(
        session, tenant_id, m.Filtro(desde=desde, hasta=ahora, connection_id=connection_id, release_id=canary, lane="canary"),
        limite=ventana * 3,
    )
    con_resultado = [c for c in recientes if not c.sin_resultado][:ventana]
    tramo = min((c.ultimo for c in con_resultado), default=ahora)
    estables = (
        await m.conversaciones_recientes(
            session, tenant_id, m.Filtro(desde=tramo, hasta=ahora, connection_id=connection_id, release_id=base, lane="stable"),
            limite=1000,
        )
        if base is not None and len(con_resultado) >= ventana
        else []
    )
    dura = ev.senal_dura(recientes, estables, u)
    # El porcentaje vigente es el de la conexión (se puede cambiar después de iniciar).
    fila = (
        await session.execute(
            text(
                "SELECT cc.canary_percent, rc.number, rb.number FROM channel_connections cc "
                "LEFT JOIN connection_releases rc ON rc.tenant_id = cc.tenant_id AND rc.id = :canary "
                "LEFT JOIN connection_releases rb ON rb.tenant_id = cc.tenant_id AND rb.id = :base "
                "WHERE cc.tenant_id = :t AND cc.id = :c"
            ),
            {"t": tenant_id, "c": connection_id, "canary": canary, "base": base},
        )
    ).first()
    estado = Estado(
        release_id=canary, base_release_id=base, percent=int(fila[0] if fila else datos.get("percent") or 0),
        desde=desde, horas=horas,
        release_number=fila[1] if fila else None, base_release_number=fila[2] if fila else None,
        veredicto=veredicto, senal_dura=dura, guardado=guardado is not None, estable=estable, canary=candidato, umbrales=u,
    )
    vencido = ahora - desde >= timedelta(days=int(u["dias_manual"]))
    if dura.activa:
        estado.motivo_no_publicar = "senal_dura"
    elif veredicto.estado == "peor":
        estado.motivo_no_publicar = "peor"
    elif veredicto.estado == "sin_muestra" and not vencido:
        estado.motivo_no_publicar = "sin_muestra"
    estado.puede_publicar = estado.motivo_no_publicar is None
    return estado


async def _guardar_veredicto(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, estado: Estado, *, actor: str
) -> None:
    if estado.guardado or estado.veredicto.estado == "sin_muestra":
        return
    await registrar_evento(
        session, tenant_id, connection_id, estado.release_id, "canary_evaluated", actor,
        {
            "veredicto": estado.veredicto.estado,
            "motivos": estado.veredicto.motivos,
            "advertencias": estado.veredicto.advertencias,
            "detalle": estado.veredicto.detalle,
        },
    )
    estado.guardado = True


async def estado(tenant_id: uuid.UUID, connection_id: uuid.UUID, *, ahora: datetime | None = None) -> Estado | None:
    """El canary de la conexión y su evaluación, o None si no hay. No escribe nada."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT canary_release_id FROM channel_connections WHERE tenant_id = :t AND id = :c "
                    "AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).first()
        if fila is None:
            from core.releases.build import ConexionNoEncontrada

            raise ConexionNoEncontrada(str(connection_id))
        if fila[0] is None:
            return None
        return await evaluar(session, tenant_id, connection_id, fila[0], ahora or datetime.now(UTC))


async def _veredicto_posterior(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID, desde: datetime) -> bool:
    """¿Llegó un veredicto de pass^k (`approved`/`rejected`) de la release después de iniciar el canary?"""
    fila = (
        await session.execute(
            text(
                "SELECT 1 FROM release_events WHERE tenant_id = :t AND release_id = :r "
                "AND type IN ('approved', 'rejected') AND created_at > :desde LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id, "desde": desde},
        )
    ).first()
    return fila is not None


_MENSAJES = {
    "senal_dura": "La versión en prueba está teniendo fallos con los clientes: no se puede publicar.",
    "peor": "La versión en prueba quedó peor que la actual: no se puede publicar.",
    "sin_muestra": "La versión en prueba todavía no junta la muestra mínima (200 conversaciones y 72 h).",
}


async def publicar(tenant_id: uuid.UUID, connection_id: uuid.UUID, *, actor: str, ahora: datetime | None = None) -> Any:
    """Publica para todos la versión en prueba (ver el docstring del módulo)."""
    _exigir_scope(tenant_id)
    rechazo: ConflictoDeRelease | None = None
    async with get_session() as session:
        activa, canary = await _sin_canary(session, tenant_id, connection_id)
        inicio_visto, _datos = await _inicio(session, tenant_id, canary)
        estado_ = await evaluar(session, tenant_id, connection_id, canary, ahora or datetime.now(UTC))
        # El veredicto se guarda aunque no se pueda publicar: el 409 se lanza después del commit.
        await _guardar_veredicto(session, tenant_id, connection_id, estado_, actor=actor)
        if not estado_.puede_publicar:
            motivo = estado_.motivo_no_publicar or "sin_muestra"
            rechazo = ConflictoDeRelease(
                f"canary_{motivo}", _MENSAJES[motivo],
                {"conversaciones": estado_.canary.conversaciones, "horas": round(estado_.horas, 1)},
            )
    if rechazo is not None:
        raise rechazo
    async with get_session() as session:
        # Se vuelve a tomar el lock y a confirmar que el canary sigue siendo el mismo.
        activa, canary_ahora = await _sin_canary(session, tenant_id, connection_id)
        desde, datos = await _inicio(session, tenant_id, canary)
        if canary_ahora != canary or desde != inicio_visto:
            raise ConflictoDeRelease("canary_cambiado", "La versión en prueba cambió mientras se publicaba.")
        # El veredicto ya está guardado; esto solo vuelve a mirar la señal dura bajo el lock.
        final = await evaluar(session, tenant_id, connection_id, canary, ahora or datetime.now(UTC))
        if not final.puede_publicar:
            motivo = final.motivo_no_publicar or "sin_muestra"
            raise ConflictoDeRelease(f"canary_{motivo}", _MENSAJES[motivo])
        # El gate guardado al iniciar vale si la base sigue siendo la activa y no llegó un veredicto de
        # pass^k después; si no, `promover_en` lo recalcula.
        heredado: dict[str, Any] | None = datos
        if str(datos.get("base_release_id")) != str(activa) or await _veredicto_posterior(session, tenant_id, canary, desde):
            heredado = None
        await session.execute(
            text(
                "UPDATE channel_connections SET canary_release_id = NULL, canary_percent = 0 "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant_id, "c": connection_id},
        )
        cambio = await promover_en(
            session, tenant_id, connection_id, canary, activa, actor=actor, motivo="canary",
            gate_heredado=heredado, extra={"desde_canary": True, "veredicto": final.veredicto.estado},
        )
    log.info("release_canary_publicado", connection_id=str(connection_id), veredicto=estado_.veredicto.estado)
    return cambio


__all__ = [
    "CANARY_DISPONIBLE",
    "Estado",
    "cambiar_porcentaje",
    "detener",
    "detener_todos",
    "estado",
    "evaluar",
    "iniciar",
    "publicar",
]
