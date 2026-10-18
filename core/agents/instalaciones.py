"""Instalaciones de habilidades con scope de conexión (ADR-0118 §2, HU-AOS-16 parte 1).

`pack_installations` (0069) guarda qué habilidad está instalada para el negocio (`scope =
'tenant'`) o solo para una conexión (`scope = 'connection'`). En esta parte de la HU:

- el scope de NEGOCIO sigue siendo `enabled_business_packs(spec activo)` (ADR-0019): no se
  escriben filas `tenant` (la fila `core` de secretos, ADR-0118 §5, es aparte);
- las filas `connection` las escribe la API (`/api/v1/admin/skills/installations`) y las lee el
  build de releases (`core/releases/build.py`): si la conexión tiene instalaciones propias
  vivas, la release congela SOLO esas (reemplazo entero, sin merge); si no, las del negocio.
  Precondición: el pack está habilitado en el negocio (la activación del código, la config y
  las tools siguen colgando de `spec.packs` hasta la parte 2). Instalar o quitar "aplica con la
  próxima release" de la conexión (ADR-0117 §2: la release fija el conjunto).
- la instalación no lleva versión: la fija la release (ADR-0118 §2, ADR-0113 §4).

Siempre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`. Sin borrado
físico: `deleted_at` (el índice único parcial permite reinstalar).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from core.agents.secretos import CORE_INSTALLATION_PACK
from core.db.models import PackInstallationRecord, utcnow
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

SCOPE_CONEXION = "connection"
SCOPE_NEGOCIO = "tenant"
ACTIVA = "active"
# Tope de instalaciones vivas por conexión (lo que lista la API y lee el build).
MAX_POR_CONEXION = 50


class InstalacionNoEncontrada(LookupError):
    pass


class ConexionNoEncontrada(LookupError):
    pass


class ConflictoInstalacion(RuntimeError):
    """Regla que el pedido rompe. `code` es estable para la API."""

    def __init__(self, code: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.code = code


@dataclass(frozen=True)
class InstalacionVista:
    id: uuid.UUID
    pack_id: str
    scope: str
    connection_id: uuid.UUID | None
    status: str
    installed_at: datetime


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("instalaciones: fuera del tenant_scope del negocio")


def _vista(fila: PackInstallationRecord) -> InstalacionVista:
    return InstalacionVista(
        id=fila.id,
        pack_id=fila.pack_id,
        scope=fila.scope,
        connection_id=fila.connection_id,
        status=fila.status,
        installed_at=fila.installed_at,
    )


async def _conexion_conversacional(session, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    fila = (
        await session.execute(
            text(
                "SELECT ct.produces_conversations FROM channel_connections cc "
                "LEFT JOIN connection_types ct ON ct.id = cc.type_id "
                "WHERE cc.tenant_id = :t AND cc.id = :c AND cc.deleted_at IS NULL FOR UPDATE OF cc"
            ),
            {"t": tenant_id, "c": connection_id},
        )
    ).first()
    if fila is None:
        raise ConexionNoEncontrada(str(connection_id))
    if not fila[0]:
        raise ConflictoInstalacion(
            "connection_not_conversational", "Las integraciones no tienen habilidades instaladas por conexión."
        )


async def instaladas_en_conexion(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> list[str]:
    """Packs con instalación propia ACTIVA y viva en la conexión (sin `core`), ordenados. Vacío
    si la conexión no tiene instalaciones propias: aplica lo del negocio."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        filas = (
            await session.execute(
                select(PackInstallationRecord.pack_id)
                .where(
                    PackInstallationRecord.tenant_id == tenant_id,
                    PackInstallationRecord.scope == SCOPE_CONEXION,
                    PackInstallationRecord.connection_id == connection_id,
                    PackInstallationRecord.status == ACTIVA,
                    PackInstallationRecord.pack_id != CORE_INSTALLATION_PACK,
                    PackInstallationRecord.deleted_at.is_(None),
                )
                .order_by(PackInstallationRecord.pack_id)
                .limit(MAX_POR_CONEXION)
            )
        ).all()
    return [f[0] for f in filas]


async def listar(tenant_id: uuid.UUID, connection_id: uuid.UUID | None = None) -> list[InstalacionVista]:
    """Instalaciones vivas del negocio (sin la `core` de secretos), opcionalmente de una conexión."""
    _exigir_scope(tenant_id)
    query = (
        select(PackInstallationRecord)
        .where(
            PackInstallationRecord.tenant_id == tenant_id,
            PackInstallationRecord.pack_id != CORE_INSTALLATION_PACK,
            PackInstallationRecord.deleted_at.is_(None),
        )
        .order_by(PackInstallationRecord.scope, PackInstallationRecord.pack_id, PackInstallationRecord.installed_at)
        .limit(500)
    )
    if connection_id is not None:
        query = query.where(PackInstallationRecord.connection_id == connection_id)
    async with get_session() as session:
        filas = (await session.execute(query)).scalars().all()
    return [_vista(f) for f in filas]


async def instalar_en_conexion(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    pack_id: str,
    *,
    habilitados: set[str],
    actor: uuid.UUID | None = None,
) -> tuple[InstalacionVista, bool]:
    """Instala `pack_id` solo para la conexión. Devuelve `(instalación, creada)`: si ya estaba
    viva, la misma (idempotente). `habilitados`: los packs del negocio (precondición)."""
    _exigir_scope(tenant_id)
    if pack_id == CORE_INSTALLATION_PACK:
        raise ConflictoInstalacion("pack_reserved", "El core no se instala: es plomería del framework.")
    if pack_id not in habilitados:
        raise ConflictoInstalacion(
            "pack_not_enabled", "Habilita la habilidad en el negocio antes de instalarla en una conexión."
        )
    async with get_session() as session:
        await _conexion_conversacional(session, tenant_id, connection_id)
        existente = (
            await session.execute(
                select(PackInstallationRecord).where(
                    PackInstallationRecord.tenant_id == tenant_id,
                    PackInstallationRecord.scope == SCOPE_CONEXION,
                    PackInstallationRecord.connection_id == connection_id,
                    PackInstallationRecord.pack_id == pack_id,
                    PackInstallationRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            return _vista(existente), False
        # El trigger contra plugins privados ajenos lanza P0001 (no `IntegrityError`): se mira
        # antes, con el mismo mensaje que un pack no disponible.
        dueno = (
            await session.execute(
                text("SELECT owner_tenant_id FROM plugins WHERE id = :p"), {"p": pack_id}
            )
        ).first()
        if dueno is None or (dueno[0] is not None and dueno[0] != tenant_id):
            raise ConflictoInstalacion("pack_unavailable", "La habilidad no está disponible para este negocio.")
        # Mismo conteo que `instaladas_en_conexion`, en esta sesión (con la conexión bloqueada).
        vivas = (
            await session.execute(
                select(func.count())
                .select_from(PackInstallationRecord)
                .where(
                    PackInstallationRecord.tenant_id == tenant_id,
                    PackInstallationRecord.scope == SCOPE_CONEXION,
                    PackInstallationRecord.connection_id == connection_id,
                    PackInstallationRecord.pack_id != CORE_INSTALLATION_PACK,
                    PackInstallationRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        if vivas >= MAX_POR_CONEXION:
            raise ConflictoInstalacion("too_many", f"La conexión ya tiene {MAX_POR_CONEXION} habilidades.")
        fila = PackInstallationRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            pack_id=pack_id,
            scope=SCOPE_CONEXION,
            connection_id=connection_id,
            status=ACTIVA,
            installed_by=actor,
        )
        session.add(fila)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Dos instalaciones a la vez (índice único parcial), un pack sin registrar en
            # `plugins` o el plugin privado de otro negocio (trigger): mismo mensaje, sin revelar.
            log.info("installation_rejected", pack_id=pack_id, error=type(exc).__name__)
            raise ConflictoInstalacion("pack_unavailable", "La habilidad no está disponible para este negocio.") from None
        vista = _vista(fila)
    log.info("installation_created", pack_id=pack_id, connection_id=str(connection_id))
    return vista, True


async def desinstalar(
    tenant_id: uuid.UUID, installation_id: uuid.UUID, *, actor: uuid.UUID | None = None
) -> InstalacionVista:
    """Borrado lógico de una instalación de conexión. La `core` de secretos no se toca."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                select(PackInstallationRecord)
                .where(
                    PackInstallationRecord.tenant_id == tenant_id,
                    PackInstallationRecord.id == installation_id,
                    PackInstallationRecord.pack_id != CORE_INSTALLATION_PACK,
                    PackInstallationRecord.scope == SCOPE_CONEXION,
                    PackInstallationRecord.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if fila is None:
            raise InstalacionNoEncontrada(str(installation_id))
        fila.deleted_at = utcnow()
        await session.flush()
        vista = _vista(fila)
    log.info(
        "installation_removed",
        pack_id=vista.pack_id,
        connection_id=str(vista.connection_id),
        actor=str(actor) if actor else None,
    )
    return vista


__all__ = [
    "ACTIVA",
    "MAX_POR_CONEXION",
    "SCOPE_CONEXION",
    "SCOPE_NEGOCIO",
    "ConexionNoEncontrada",
    "ConflictoInstalacion",
    "InstalacionNoEncontrada",
    "InstalacionVista",
    "desinstalar",
    "instaladas_en_conexion",
    "instalar_en_conexion",
    "listar",
]
