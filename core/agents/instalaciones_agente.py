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
    if agente.kind == "core":  # ADR-0205 §5: las habilidades del Coordinador son fijas del core
        raise ConflictoInstalacion("coordinador_inmutable", "Las habilidades del Coordinador vienen fijas del sistema.")
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


# Estados de una instalación que cuentan como «la tiene» (ADR-0206 §5): `pending_config` (Pagos sin cuenta) sí; una
# instalación `disabled` o con `error`, no.
_ESTADOS_INSTALADA = ("active", "pending_config")
CORE_PACK_ID = "core"


async def packs_del_negocio(tenant_id: uuid.UUID | None = None, *, con_core: bool = True) -> set[str]:
    """Las habilidades del negocio: la ÚNICA lectura fuera del turno (ADR-0206 §5). Con `con_core` (por defecto)
    incluye `core`, como `enabled_packs`; sin él es la cuenta del plan. `tenant_id` por defecto es el del
    `tenant_scope`.

    Una habilidad es del negocio solo si algún agente activo la tiene instalada (decisión del dueño del
    2026-11-22, HU-AOS-91): es `packs_instalados`. `agent_specs.packs` ya no se lee ni se escribe."""
    from core.shared.tenant import get_current_tenant_id

    tid = tenant_id or get_current_tenant_id()
    if tid is None:
        raise PermissionError("packs_del_negocio fuera de un tenant_scope")
    return await packs_instalados(tid, con_core=con_core)


async def packs_instalados(tenant_id: uuid.UUID, *, con_core: bool = True) -> set[str]:
    """Las habilidades que tiene instaladas algún agente activo (no archivado ni borrado) del negocio: la fuente de
    `packs_del_negocio` desde la parte C de HU-AOS-91 (ADR-0206 §5)."""
    tid = tenant_id
    _exigir_scope(tid)
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT DISTINCT i.pack_id FROM pack_installations i "
                    "JOIN agents a ON a.tenant_id = i.tenant_id AND a.id = i.agent_id "
                    "WHERE i.tenant_id = :t AND i.scope = 'agent' AND i.deleted_at IS NULL "
                    "AND i.status = ANY(:estados) AND a.deleted_at IS NULL AND a.status = 'active'"
                ),
                {"t": tid, "estados": list(_ESTADOS_INSTALADA)},
            )
        ).scalars().all()
    packs = {str(p) for p in filas} - {CORE_INSTALLATION_PACK}
    return packs | {CORE_PACK_ID} if con_core else packs


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
    "backfill_habilidades_por_agente",
    "config_del_agente",
    "desinstalar_de_agente",
    "guardar_config_del_agente",
    "instaladas_en_agente",
    "instalar_en_agente",
]


CONOCIMIENTO = "pack_knowledge"


async def _usa_conocimiento(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    """¿El borrador o alguna versión del agente declara la acción de búsqueda en documentos?"""
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT 1 FROM agent_drafts WHERE tenant_id = :t AND agent_id = :a "
                    "AND content::text LIKE '%buscar_conocimiento%' "
                    "UNION ALL SELECT 1 FROM agent_versions WHERE tenant_id = :t AND agent_id = :a "
                    "AND content::text LIKE '%buscar_conocimiento%' LIMIT 1"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).first()
    return fila is not None


async def _packs_del_spec_v2(tenant_id: uuid.UUID) -> set[str]:
    """Los `packs` del AgentSpec v2 activo, leídos con SQL de `agent_specs` (sin el modelo, que ya no existe): solo
    para el precheck y el backfill. Es lo que se corre si la guarda de la migración 0157 la frena; después de 0157 la
    tabla no existe y devuelve vacío."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        # La migración 0157 borra la tabla: sin ella no queda nada que comparar (es lo que el precheck ya miró).
        if (await session.execute(text("SELECT to_regclass('agent_specs')"))).scalar() is None:
            return set()
        fila = (
            await session.execute(
                text(
                    "SELECT spec -> 'packs' FROM agent_specs WHERE tenant_id = :t AND is_active AND deleted_at IS NULL "
                    "LIMIT 1"
                ),
                {"t": tenant_id},
            )
        ).scalar_one_or_none()
    return {str(p) for p in fila if isinstance(p, str)} - {CORE_PACK_ID} if isinstance(fila, list) else set()


async def backfill_habilidades_por_agente(
    tenant_ids: list[uuid.UUID] | None = None, *, aplicar: bool = False
) -> list[dict[str, object]]:
    """`agent-core db habilidades-por-agente` (ADR-0190, HU-AOS-65): instala en cada agente especialista vivo las
    habilidades de su plantilla (por `key` o `source_template`) y las opcionales que el negocio ya tiene, SIN borrar
    nada. Idempotente: lo que ya está instalado no se toca. Pagos nace `pending_config` (falta elegir la cuenta).
    Dry-run por defecto. Devuelve una fila por agente con lo que instaló (o instalaría)."""
    from core.presets import PresetError, load_plantilla
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        negocios = (
            await session.execute(
                text(
                    "SELECT id, slug FROM tenants WHERE deleted_at IS NULL "
                    "AND (cast(:ids AS uuid[]) IS NULL OR id = ANY(cast(:ids AS uuid[]))) ORDER BY slug"
                ),
                {"ids": [str(t) for t in tenant_ids] if tenant_ids else None},
            )
        ).all()
    filas: list[dict[str, object]] = []
    for tid, slug in negocios:
        async with tenant_scope(tid):
            del_negocio = await _packs_del_spec_v2(tid) | {CORE_PACK_ID}
            async with get_session() as session:
                agentes = (
                    await session.execute(
                        select(AgentRecord).where(
                            AgentRecord.tenant_id == tid,
                            AgentRecord.kind == "specialist",
                            AgentRecord.status == "active",
                            AgentRecord.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
            for agente in agentes:
                plantilla = None
                for clave in (agente.source_template, agente.key):
                    if not clave:
                        continue
                    try:
                        plantilla = load_plantilla(clave)
                        break
                    except PresetError:
                        continue
                if plantilla is None:
                    continue
                propias = set(await instaladas_en_agente(tid, agente.id, solo_activas=False))
                quiere = set(plantilla.habilidades) | (set(plantilla.opcionales) & del_negocio)
                if CONOCIMIENTO in del_negocio and await _usa_conocimiento(tid, agente.id):
                    # Un agente que ya busca en documentos no puede perderlo en su próxima release.
                    quiere.add(CONOCIMIENTO)
                quiere -= propias
                quiere &= del_negocio | set(plantilla.habilidades)
                if aplicar:
                    for pack in sorted(quiere):
                        await instalar_en_agente(tid, agente.id, pack, requiere_config=pack == "pack_payments")
                filas.append({"negocio": slug, "agente": agente.key, "instaladas": sorted(quiere), "ya": sorted(propias)})
    log.info("habilidades_por_agente_backfill", aplicar=aplicar, agentes=len(filas))
    return filas


async def precheck_packs_del_negocio(
    tenant_ids: list[uuid.UUID] | None = None, *, aplicar: bool = False, quitar_sobrantes: bool = False
) -> list[dict[str, object]]:
    """`agent-core db packs-sin-instalacion` (ADR-0206 §5, HU-AOS-91): compara, por negocio, las habilidades de
    `agent_specs.packs` con las que tienen instaladas sus agentes activos (la fuente nueva).

    - `faltan`: en `agent_specs` y en ningún agente. Al cambiar de fuente se perderían. Con `aplicar`, una habilidad
      transversal (voz, conocimiento, pagos) se instala en todos los especialistas activos del negocio; una de
      especialidad no se instala sola (necesita su agente) y queda reportada.
    - `sobran`: instaladas en algún agente activo y no en `agent_specs`. Al cambiar de fuente aparecerían. Solo con
      `quitar_sobrantes` se desinstalan (borrado lógico) de esos agentes.

    Dry-run por defecto. Idempotente. Se despliega HU-AOS-91 parte B con cero diferencias o con cada una resuelta."""
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        negocios = (
            await session.execute(
                text(
                    "SELECT id, slug FROM tenants WHERE deleted_at IS NULL "
                    "AND (cast(:ids AS uuid[]) IS NULL OR id = ANY(cast(:ids AS uuid[]))) ORDER BY slug"
                ),
                {"ids": [str(t) for t in tenant_ids] if tenant_ids else None},
            )
        ).all()
    filas: list[dict[str, object]] = []
    for tid, slug in negocios:
        async with tenant_scope(tid):
            del_spec = await _packs_del_spec_v2(tid)
            instaladas = await packs_instalados(tid, con_core=False)
            faltan, sobran = sorted(del_spec - instaladas), sorted(instaladas - del_spec)
            if not faltan and not sobran:
                continue
            instalo: list[str] = []
            quito: list[str] = []
            async with get_session() as session:
                especialistas = (
                    await session.execute(
                        select(AgentRecord.id).where(
                            AgentRecord.tenant_id == tid,
                            AgentRecord.kind == "specialist",
                            AgentRecord.status == "active",
                            AgentRecord.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
            for pack in faltan:
                modulo = plugin_manager.get(pack)
                if getattr(modulo, "tipo", "") != "transversal" or not especialistas:
                    continue
                if aplicar:
                    for agent_id in especialistas:
                        await instalar_en_agente(tid, agent_id, pack, requiere_config=pack == "pack_payments")
                instalo.append(pack)
            if quitar_sobrantes:
                for pack in sobran:
                    async with get_session() as session:
                        con_pack = (
                            await session.execute(
                                text(
                                    "SELECT i.agent_id FROM pack_installations i JOIN agents a ON a.tenant_id = "
                                    "i.tenant_id AND a.id = i.agent_id WHERE i.tenant_id = :t AND i.scope = 'agent' "
                                    "AND i.pack_id = :p AND i.deleted_at IS NULL AND a.deleted_at IS NULL "
                                    "AND a.status = 'active'"
                                ),
                                {"t": tid, "p": pack},
                            )
                        ).scalars().all()
                    if aplicar:
                        for agent_id in con_pack:
                            await desinstalar_de_agente(tid, agent_id, pack)
                    quito.append(pack)
            filas.append(
                {"negocio": slug, "faltan": faltan, "sobran": sobran, "instala": instalo, "quita": quito}
            )
    return filas
