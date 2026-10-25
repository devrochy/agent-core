"""Promoción, rollback en un clic y lectura de las releases de una conexión (ADR-0117 §5 y §6, HU-AOS-14).

- **Promover** cambia el puntero `channel_connections.active_release_id` y deja un evento
  `promoted` (`from_release_id`, `reason`). La promoción es total: el canary con porcentaje
  llega en HU-AOS-32 y aquí no se toca `canary_release_id`. Antes pasa `gate_aprobado`, el gate
  de pass^k (HU-AOS-21): calcula qué procedimientos tocó la candidata frente a la activa y lo
  anota en el evento (`gate`, `gate_tocados`). Hasta el corredor de pass^k no bloquea.
- **Rollback** vuelve el puntero a una release anterior: por defecto, la más reciente que estuvo
  activa (tiene `promoted`), no es la activa y no fue retirada por `rolled_back` después de ese
  `promoted`. Así dos clics seguidos no rebotan entre dos releases. Deja `rolled_back` en la
  release que se deja (`to_release_id`, `reason`) y `promoted` en la de destino
  (`reason = 'rollback'`).
- El efecto es en el siguiente turno de cada conversación (≤ 5 s): el kernel lee el puntero en
  cada turno y las conversaciones de una release retirada por rollback cambian a mitad de
  episodio (`core.kernel.release_del_turno`).

Ambas bloquean la fila de la conexión (`FOR UPDATE`): se serializan entre sí y con el insert de
un build. Corren dentro del `tenant_scope` del negocio y además filtran por `tenant_id`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.releases.build import ConexionNoEncontrada, registrar_evento
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Eventos de ciclo de vida: los que deciden si una release está "en uso" o retirada. Los del
# gate (`evaluated`, `approved`, `rejected`) no cuentan (ADR-0117 §4, HU-AOS-21).
CICLO_DE_VIDA = ("promoted", "canary_started", "rolled_back", "canary_stopped", "invalid")
# Tras uno de estos, las conversaciones de la release cambian en su siguiente turno.
RETIRAN = frozenset({"rolled_back", "canary_stopped", "invalid"})


class ReleaseNoEncontrada(LookupError):
    pass


class ConflictoDeRelease(RuntimeError):
    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.codigo = codigo


@dataclass(frozen=True)
class Cambio:
    connection_id: uuid.UUID
    from_release_id: uuid.UUID | None
    to_release_id: uuid.UUID
    gate: str


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("releases: fuera del tenant_scope del negocio")


# Exenciones del gate (adenda HU-AOS-21 de ADR-0117): las pasan los llamadores internos por
# palabra clave; la API nunca las envía y sus motivos están reservados en el router. Release 1
# no está aquí: se reconoce porque la conexión no tiene release activa.
EXENCIONES = frozenset({"migrar_a_v3", "qa_datos", "hotfix_yanked"})
# Tope de motivos anotados en el evento (el detalle completo irá en el veredicto, parte 3).
_MAX_TOCADOS_EN_EVENTO = 50


async def gate_aprobado(
    session: Any,
    tenant_id: uuid.UUID,
    release_id: uuid.UUID,
    activa: uuid.UUID | None,
    *,
    exencion: str | None = None,
    forzado: bool = False,
) -> tuple[bool, str, dict[str, str]]:
    """El gate de promoción (HU-AOS-21). Devuelve `(aprobado, gate, tocados)`.

    Lee, no mide: corre dentro del `FOR UPDATE` de `promover` y solo cree el último veredicto de
    la candidata medido contra la release activa de ahora (`base_release_id`). Valores de `gate`:
    `exento_release_1`, `exento_<exencion>`, `forzado`, `sin_cambios`, `aprobado`, `rechazado` y
    `sin_evaluar`. Sin veredicto, la candidata pasa con `sin_evaluar` mientras el corredor no
    cierre el gate (decisión del dueño, 2026-10-25); un veredicto `rejected` la bloquea."""
    if activa is None:
        return True, "exento_release_1", {}
    if forzado:
        return True, "forzado", {}
    if exencion is not None:
        if exencion not in EXENCIONES:
            raise ValueError(f"exención desconocida: {exencion}")
        if exencion == "qa_datos" and not await _es_negocio_de_prueba(session, tenant_id):
            raise ConflictoDeRelease("exencion_invalida", "Esa exención solo aplica a negocios de prueba.")
        return True, f"exento_{exencion}", {}
    from core.releases.cargar import cargar_release
    from core.releases.diferencias import procedimientos_tocados

    candidata = await cargar_release(session, tenant_id, release_id)
    base = await cargar_release(session, tenant_id, activa)
    if candidata is None:
        raise ReleaseNoEncontrada(str(release_id))
    tocados = procedimientos_tocados(candidata, base)
    if not tocados:
        return True, "sin_cambios", tocados
    from core.releases import veredicto

    previo = await veredicto.ultimo(session, tenant_id, release_id, activa)
    if previo is None:
        return True, "sin_evaluar", tocados
    if previo["tipo"] == veredicto.RECHAZADO:
        return False, "rechazado", tocados
    return True, "aprobado", tocados


async def _es_negocio_de_prueba(session: Any, tenant_id: uuid.UUID) -> bool:
    slug = (
        await session.execute(
            text("SELECT slug FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
        )
    ).scalar_one_or_none()
    return isinstance(slug, str) and slug.startswith("qa-")


async def _bloquear(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    fila = (
        await session.execute(
            text(
                "SELECT active_release_id, canary_release_id FROM channel_connections "
                "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL FOR UPDATE"
            ),
            {"t": tenant_id, "c": connection_id},
        )
    ).first()
    if fila is None:
        raise ConexionNoEncontrada(str(connection_id))
    return fila[0], fila[1]


async def _release(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID
) -> dict[str, Any]:
    fila = (
        (
            await session.execute(
                text(
                    "SELECT id, number, origin, manifest_hash FROM connection_releases "
                    "WHERE tenant_id = :t AND connection_id = :c AND id = :r"
                ),
                {"t": tenant_id, "c": connection_id, "r": release_id},
            )
        )
        .mappings()
        .first()
    )
    if fila is None:
        raise ReleaseNoEncontrada(str(release_id))
    return dict(fila)


async def _exigir_utilizable(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID) -> None:
    """409 si la release fue invalidada o detenida (`rolled_back` sí se puede reactivar), o si
    fija una versión de habilidad retirada por seguridad (`yanked`, ADR-0117 §3)."""
    ultimo = (
        await session.execute(
            text(
                "SELECT type FROM release_events WHERE tenant_id = :t AND release_id = :r "
                "AND type = ANY(:tipos) ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id, "tipos": list(CICLO_DE_VIDA)},
        )
    ).scalar_one_or_none()
    if ultimo in RETIRAN - {"rolled_back"}:
        raise ConflictoDeRelease(
            "release_retirada", "Esa versión fue retirada y no se puede activar."
        )
    yanked = (
        await session.execute(
            text(
                "SELECT 1 FROM connection_releases r "
                "CROSS JOIN LATERAL jsonb_array_elements(r.manifest->'skills') s "
                "JOIN pack_versions pv ON pv.pack_id = s->>'pack_id' AND pv.version = s->>'version' "
                "WHERE r.tenant_id = :t AND r.id = :r AND pv.yanked LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id},
        )
    ).first()
    if yanked is not None:
        raise ConflictoDeRelease(
            "habilidad_retirada", "Esa versión usa una habilidad retirada por seguridad."
        )


async def _apuntar(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID
) -> None:
    await session.execute(
        text(
            "UPDATE channel_connections SET active_release_id = :r WHERE tenant_id = :t AND id = :c"
        ),
        {"t": tenant_id, "c": connection_id, "r": release_id},
    )


async def promover(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    actor: str,
    motivo: str = "manual",
    exencion: str | None = None,
    forzado_por_master: str | None = None,
) -> Cambio:
    """Activa la release (promoción total). 409 si ya es la activa o el gate la rechaza.

    `exencion` solo la pasan los llamadores internos (`migrar_a_v3`, `qa_datos`,
    `hotfix_yanked`); la API nunca la envía. `forzado_por_master` es el motivo escrito con el
    que un master salta el gate (la API lo exige de un master); queda en el evento."""
    _exigir_scope(tenant_id)
    forzado = bool(forzado_por_master and forzado_por_master.strip())
    async with get_session() as session:
        activa, _ = await _bloquear(session, tenant_id, connection_id)
        release = await _release(session, tenant_id, connection_id, release_id)
        if activa == release_id:
            raise ConflictoDeRelease("ya_activa", "Esa versión ya es la activa.")
        await _exigir_utilizable(session, tenant_id, release_id)
        aprobado, gate, tocados = await gate_aprobado(
            session, tenant_id, release_id, activa, exencion=exencion, forzado=forzado
        )
        if not aprobado:
            raise ConflictoDeRelease(
                "gate_rechazado",
                "La versión no pasó el gate de pass^k: algún procedimiento quedó bajo el umbral "
                "o peor que la versión activa.",
            )
        await _apuntar(session, tenant_id, connection_id, release_id)
        datos: dict[str, Any] = {
            "from_release_id": str(activa) if activa else None,
            "reason": motivo[:40],
            "gate": gate,
            "gate_tocados": dict(sorted(tocados.items())[:_MAX_TOCADOS_EN_EVENTO]),
        }
        if forzado:
            datos["gate_motivo"] = str(forzado_por_master).strip()[:200]
        await registrar_evento(session, tenant_id, connection_id, release_id, "promoted", actor, datos)
        if forzado:
            from core.db.models import AuditLog

            # En la misma transacción que la promoción: no queda un forzado sin auditoría.
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=actor,
                    action="release_gate_forzado",
                    input={
                        "connection_id": str(connection_id),
                        "release_id": str(release_id),
                        "motivo": datos["gate_motivo"],
                    },
                )
            )
    log.info(
        "release_promoted",
        connection_id=str(connection_id),
        number=release["number"],
        reason=motivo,
        gate=gate,
        procedimientos_tocados=len(tocados),
    )
    return Cambio(connection_id, activa, release_id, gate)


async def destino_de_rollback(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, activa: uuid.UUID
) -> uuid.UUID | None:
    """La release más reciente que estuvo activa, no es la activa y no se retiró por
    `rolled_back` después de su último `promoted`."""
    return (
        await session.execute(
            text(
                "WITH ultima AS ("
                "  SELECT release_id, max(created_at) AS promovida FROM release_events "
                "  WHERE tenant_id = :t AND connection_id = :c AND type = 'promoted' GROUP BY release_id) "
                "SELECT u.release_id FROM ultima u "
                "WHERE u.release_id <> :activa AND NOT EXISTS ("
                "  SELECT 1 FROM release_events e WHERE e.tenant_id = :t AND e.release_id = u.release_id "
                "  AND e.type = ANY(:retiran) AND e.created_at >= u.promovida) "
                "ORDER BY u.promovida DESC LIMIT 1"
            ),
            {"t": tenant_id, "c": connection_id, "activa": activa, "retiran": sorted(RETIRAN)},
        )
    ).scalar_one_or_none()


async def rollback(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    actor: str,
    to_release_id: uuid.UUID | None = None,
    motivo: str = "manual",
) -> Cambio:
    """Vuelve a una release anterior (cambio de puntero). 409 sin release activa, sin destino
    posible, o si el destino es la activa o nunca estuvo activa."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        activa, _ = await _bloquear(session, tenant_id, connection_id)
        if activa is None:
            raise ConflictoDeRelease(
                "sin_release_activa", "La conexión no tiene una versión activa."
            )
        if to_release_id is None:
            destino = await destino_de_rollback(session, tenant_id, connection_id, activa)
            if destino is None:
                raise ConflictoDeRelease(
                    "sin_version_anterior", "No hay una versión anterior a la cual volver."
                )
        else:
            destino = to_release_id
            await _release(session, tenant_id, connection_id, destino)
            if destino == activa:
                raise ConflictoDeRelease("ya_activa", "Esa versión ya es la activa.")
            estuvo = (
                await session.execute(
                    text(
                        "SELECT 1 FROM release_events WHERE tenant_id = :t AND release_id = :r "
                        "AND type = 'promoted' LIMIT 1"
                    ),
                    {"t": tenant_id, "r": destino},
                )
            ).first()
            if estuvo is None:
                raise ConflictoDeRelease(
                    "nunca_activa", "Esa versión nunca estuvo activa: promuévela."
                )
        await _exigir_utilizable(session, tenant_id, destino)
        await _apuntar(session, tenant_id, connection_id, destino)
        await registrar_evento(
            session,
            tenant_id,
            connection_id,
            activa,
            "rolled_back",
            actor,
            {"to_release_id": str(destino), "reason": motivo[:40]},
        )
        await registrar_evento(
            session,
            tenant_id,
            connection_id,
            destino,
            "promoted",
            actor,
            {"from_release_id": str(activa), "reason": "rollback"},
        )
    log.info("release_rolled_back", connection_id=str(connection_id), reason=motivo)
    return Cambio(connection_id, activa, destino, "rollback")


# ------------------------------------------------------------------ lectura
@dataclass(frozen=True)
class ReleaseVista:
    id: uuid.UUID
    number: int
    origin: str
    manifest_hash: str
    created_at: datetime
    active: bool
    canary: bool
    last_event: str | None
    last_event_at: datetime | None


async def _punteros(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[Any, Any]:
    fila = (
        await session.execute(
            text(
                "SELECT active_release_id, canary_release_id FROM channel_connections "
                "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
            ),
            {"t": tenant_id, "c": connection_id},
        )
    ).first()
    if fila is None:
        raise ConexionNoEncontrada(str(connection_id))
    return fila[0], fila[1]


async def listar(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, *, limite: int = 50, antes_de: int | None = None
) -> list[ReleaseVista]:
    """Releases de la conexión, de la más nueva a la más vieja (keyset por `number`)."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        activa, canary = await _punteros(session, tenant_id, connection_id)
        filas = (
            await session.execute(
                text(
                    "SELECT r.id, r.number, r.origin, r.manifest_hash, r.created_at, e.type, e.created_at "
                    "FROM connection_releases r "
                    "LEFT JOIN LATERAL (SELECT type, created_at FROM release_events ev "
                    "  WHERE ev.tenant_id = r.tenant_id AND ev.release_id = r.id "
                    "  ORDER BY ev.created_at DESC LIMIT 1) e ON true "
                    "WHERE r.tenant_id = :t AND r.connection_id = :c AND (cast(:antes AS integer) IS NULL OR r.number < cast(:antes AS integer)) "
                    "ORDER BY r.number DESC LIMIT :lim"
                ),
                {
                    "t": tenant_id,
                    "c": connection_id,
                    "antes": antes_de,
                    "lim": max(1, min(limite, 200)),
                },
            )
        ).all()
    return [
        ReleaseVista(f[0], f[1], f[2], f[3], f[4], f[0] == activa, f[0] == canary, f[5], f[6])
        for f in filas
    ]


async def obtener(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID
) -> tuple[ReleaseVista, dict[str, Any], list[dict[str, Any]]]:
    """La release, su manifiesto (completo: quien llama lo resume) y sus eventos."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        activa, canary = await _punteros(session, tenant_id, connection_id)
        fila = (
            await session.execute(
                text(
                    "SELECT id, number, origin, manifest_hash, created_at, manifest FROM connection_releases "
                    "WHERE tenant_id = :t AND connection_id = :c AND id = :r"
                ),
                {"t": tenant_id, "c": connection_id, "r": release_id},
            )
        ).first()
        if fila is None:
            raise ReleaseNoEncontrada(str(release_id))
        eventos = [
            dict(e)
            for e in (
                await session.execute(
                    text(
                        "SELECT type, data, actor, created_at FROM release_events "
                        "WHERE tenant_id = :t AND release_id = :r ORDER BY created_at, id LIMIT 500"
                    ),
                    {"t": tenant_id, "r": release_id},
                )
            ).mappings()
        ]
    ultimo = eventos[-1] if eventos else None
    vista = ReleaseVista(
        fila[0],
        fila[1],
        fila[2],
        fila[3],
        fila[4],
        fila[0] == activa,
        fila[0] == canary,
        ultimo["type"] if ultimo else None,
        ultimo["created_at"] if ultimo else None,
    )
    return vista, fila[5], eventos


__all__ = [
    "CICLO_DE_VIDA",
    "RETIRAN",
    "Cambio",
    "ConflictoDeRelease",
    "ReleaseNoEncontrada",
    "ReleaseVista",
    "destino_de_rollback",
    "gate_aprobado",
    "listar",
    "obtener",
    "promover",
    "rollback",
]
