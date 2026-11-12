"""Habilidades instaladas por agente (ADR-0190 §2-§3, HU-AOS-61).

Una fila de `pack_installations` con `scope = 'agent'` dice que la habilidad `pack_id` es de UN agente del negocio. Su
`config_encrypted` guarda la configuración de ESE agente (AES-GCM con AAD `(tenant_id, installation_id)`, el mismo
helper que la instalación `core`). Lo que es del negocio (catálogo, numeración, cuentas de transferencia…) sigue en
`plugin_configs`; el reparto por campo es el inventario de SPEC-44.

Reglas:
- el agente debe existir en ESTE negocio, vivo y no archivado (la FK compuesta lo garantiza en la base; aquí se mira
  antes para dar un error claro);
- instalar es idempotente: si ya está viva, devuelve la misma;
- quitar es borrado lógico (invariante 8): el índice único parcial deja reinstalar;
- un negocio no instala el plugin privado de otro (mismo chequeo que las instalaciones por conexión).

Siempre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from core.agents.instalaciones import (
    ACTIVA,
    ConflictoInstalacion,
    InstalacionNoEncontrada,
    InstalacionVista,
    _exigir_scope,
)
from core.agents.secretos import CORE_INSTALLATION_PACK, aad_instalacion
from core.db.models import AgentRecord, PackInstallationRecord, utcnow
from core.db.session import get_session
from core.shared.crypto import seal, unseal
from core.shared.logger import get_logger

log = get_logger(__name__)

SCOPE_AGENTE = "agent"
PENDIENTE = "pending_config"
# Tope de habilidades vivas por agente: hay seis packs; el margen es para los privados del negocio.
MAX_POR_AGENTE = 30


class AgenteNoEncontrado(LookupError):
    pass


def _vista(fila: PackInstallationRecord) -> InstalacionVista:
    return InstalacionVista(
        id=fila.id,
        pack_id=fila.pack_id,
        scope=fila.scope,
        connection_id=None,
        status=fila.status,
        installed_at=fila.installed_at,
    )


async def _agente_vivo(session, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> AgentRecord:  # type: ignore[no-untyped-def]
    agente = (
        await session.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == tenant_id,
                AgentRecord.id == agent_id,
                AgentRecord.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if agente is None:
        raise AgenteNoEncontrado(str(agent_id))
    if agente.status != "active":
        raise ConflictoInstalacion("agent_archived", "El agente está archivado: no se le instalan habilidades.")
    return agente


def _de_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID, pack_id: str):  # type: ignore[no-untyped-def]
    return select(PackInstallationRecord).where(
        PackInstallationRecord.tenant_id == tenant_id,
        PackInstallationRecord.scope == SCOPE_AGENTE,
        PackInstallationRecord.agent_id == agent_id,
        PackInstallationRecord.pack_id == pack_id,
        PackInstallationRecord.deleted_at.is_(None),
    )


async def instaladas_en_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID, *, solo_activas: bool = True) -> list[str]:
    """Packs instalados y vivos en el agente, ordenados. Con `solo_activas`, los que no esperan configuración."""
    _exigir_scope(tenant_id)
    query = (
        select(PackInstallationRecord.pack_id)
        .where(
            PackInstallationRecord.tenant_id == tenant_id,
            PackInstallationRecord.scope == SCOPE_AGENTE,
            PackInstallationRecord.agent_id == agent_id,
            PackInstallationRecord.deleted_at.is_(None),
        )
        .order_by(PackInstallationRecord.pack_id)
        .limit(MAX_POR_AGENTE)
    )
    if solo_activas:
        query = query.where(PackInstallationRecord.status == ACTIVA)
    async with get_session() as session:
        filas = (await session.execute(query)).all()
    return [f[0] for f in filas]


async def instalar_en_agente(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    pack_id: str,
    *,
    requiere_config: bool = False,
    actor: uuid.UUID | None = None,
) -> tuple[InstalacionVista, bool]:
    """Instala `pack_id` en el agente. Devuelve `(instalación, creada)`. Con `requiere_config` nace
    `pending_config` hasta que se guarde su configuración del agente."""
    _exigir_scope(tenant_id)
    if pack_id == CORE_INSTALLATION_PACK:
        raise ConflictoInstalacion("pack_reserved", "El core no se instala: es plomería del framework.")
    async with get_session() as session:
        await _agente_vivo(session, tenant_id, agent_id)
        existente = (await session.execute(_de_agente(tenant_id, agent_id, pack_id))).scalar_one_or_none()
        if existente is not None:
            return _vista(existente), False
        dueno = (await session.execute(text("SELECT owner_tenant_id FROM plugins WHERE id = :p"), {"p": pack_id})).first()
        if dueno is None or (dueno[0] is not None and dueno[0] != tenant_id):
            raise ConflictoInstalacion("pack_unavailable", "La habilidad no está disponible para este negocio.")
        vivas = len(
            (
                await session.execute(
                    select(PackInstallationRecord.id).where(
                        PackInstallationRecord.tenant_id == tenant_id,
                        PackInstallationRecord.scope == SCOPE_AGENTE,
                        PackInstallationRecord.agent_id == agent_id,
                        PackInstallationRecord.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        if vivas >= MAX_POR_AGENTE:
            raise ConflictoInstalacion("too_many", f"El agente ya tiene {MAX_POR_AGENTE} habilidades.")
        fila = PackInstallationRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            pack_id=pack_id,
            scope=SCOPE_AGENTE,
            agent_id=agent_id,
            status=PENDIENTE if requiere_config else ACTIVA,
            installed_by=actor,
        )
        session.add(fila)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Dos instalaciones a la vez (índice único parcial) o un pack sin registrar: mismo mensaje.
            log.info("agent_installation_rejected", pack_id=pack_id, error=type(exc).__name__)
            raise ConflictoInstalacion("pack_unavailable", "La habilidad no está disponible para este negocio.") from None
        vista = _vista(fila)
    log.info("agent_installation_created", pack_id=pack_id, agent_id=str(agent_id))
    return vista, True


async def desinstalar_de_agente(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, pack_id: str, *, actor: uuid.UUID | None = None
) -> InstalacionVista:
    """Borrado lógico de la instalación del agente."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (await session.execute(_de_agente(tenant_id, agent_id, pack_id).with_for_update())).scalar_one_or_none()
        if fila is None:
            raise InstalacionNoEncontrada(f"{agent_id}:{pack_id}")
        fila.deleted_at = utcnow()
        await session.flush()
        vista = _vista(fila)
    log.info(
        "agent_installation_removed", pack_id=pack_id, agent_id=str(agent_id), actor=str(actor) if actor else None
    )
    return vista


async def guardar_config_del_agente(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, pack_id: str, valores: Mapping[str, object]
) -> InstalacionVista:
    """Guarda (reemplaza) la configuración del agente para `pack_id`, ya validada contra el esquema del agente del
    pack, y deja la instalación activa. Nunca se loguean los valores."""
    _exigir_scope(tenant_id)
    plano = json.dumps(dict(valores), ensure_ascii=False, sort_keys=True, default=str)
    async with get_session() as session:
        fila = (await session.execute(_de_agente(tenant_id, agent_id, pack_id).with_for_update())).scalar_one_or_none()
        if fila is None:
            raise InstalacionNoEncontrada(f"{agent_id}:{pack_id}")
        fila.config_encrypted = seal(plano, aad_instalacion(tenant_id, fila.id))
        fila.status = ACTIVA
        await session.flush()
        vista = _vista(fila)
    log.info("agent_installation_configured", pack_id=pack_id, agent_id=str(agent_id), campos=len(valores))
    return vista


async def config_del_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID, pack_id: str) -> dict[str, object] | None:
    """La configuración del agente para `pack_id`, descifrada. `None` si no está instalada o no tiene config."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (await session.execute(_de_agente(tenant_id, agent_id, pack_id))).scalar_one_or_none()
    if fila is None or not fila.config_encrypted:
        return None
    if not fila.config_encrypted.startswith("gcm:v1:"):
        # Solo AES-GCM con AAD: un Fernet (sin AAD) copiado de otra fila no se acepta.
        raise RuntimeError("instalaciones_agente: formato de cifrado no permitido")
    data = json.loads(unseal(fila.config_encrypted, aad_instalacion(tenant_id, fila.id)))
    return data if isinstance(data, dict) else None


__all__ = [
    "MAX_POR_AGENTE",
    "SCOPE_AGENTE",
    "AgenteNoEncontrado",
    "config_del_agente",
    "desinstalar_de_agente",
    "guardar_config_del_agente",
    "instaladas_en_agente",
    "instalar_en_agente",
]
