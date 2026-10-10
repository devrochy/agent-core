"""Agentes con borrador editable y versiones inmutables (ADR-0113, ADR-0117, HU-AOS-12).

- Un agente (`agents`) tiene un `kind` (`core` → `kernelspec/1`; `specialist` y `backoffice` →
  `agentspec/3`), un borrador (`agent_drafts`) y versiones (`agent_versions`, append-only).
- **Borrador:** se guarda con concurrencia optimista. `UPDATE ... WHERE lock_version = :v`; si
  otro lo cambió antes, `Conflicto` (409). Se valida con el mismo validador del CLI y de CI
  (`revisar_spec`) más las referencias a secretos: un spec inválido no se guarda.
- **Publicar:** congela el borrador en una versión con `content_hash` = sha256 del JSON
  canónico (claves ordenadas, sin espacios) del contenido ya normalizado. Mismo hash que una
  versión existente → se devuelve esa (idempotente). El número es `max + 1` con la fila del
  agente bloqueada (`FOR UPDATE`), así dos publicaciones a la vez no chocan. Publicar no
  activa nada: la release por conexión llega con HU-AOS-14.
- `publicar(..., contenido=..., legacy_agent_spec_id=...)` es también el camino del backfill
  de HU-AOS-13: misma validación y mismo hash.

Todo corre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from agentcore_contracts import AgentSpecV3, SecretRef
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from core.agents.contrato_v3 import Contrato, revisar_spec, validar_spec
from core.agents.tolerante import KEYS_RESERVADAS
from core.db.models import AgentDraftRecord, AgentRecord, AgentVersionRecord
from core.db.session import get_session
from core.kernel.procedures import Catalogo, Issue
from core.shared.tenant import get_current_tenant_id

CONTRATO_POR_KIND: dict[str, Contrato] = {
    "core": "kernelspec/1",
    "specialist": "agentspec/3",
    "backoffice": "agentspec/3",
}
KINDS = tuple(CONTRATO_POR_KIND)


class AgenteNoEncontrado(LookupError):
    pass


class Conflicto(RuntimeError):
    def __init__(self, mensaje: str, lock_version: int | None = None) -> None:
        super().__init__(mensaje)
        self.lock_version = lock_version


class ErroresDeContrato(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(f"{len(issues)} problema(s) del contrato")
        self.issues = issues


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("agentes: fuera del tenant_scope del negocio")


def content_hash(contenido: Mapping[str, Any]) -> str:
    """sha256 del JSON canónico (claves ordenadas, sin espacios, UTF-8)."""
    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode()).hexdigest()


def contenido_inicial(kind: str) -> dict[str, Any]:
    """Borrador válido de un agente nuevo: los defaults del contrato."""
    from agentcore_contracts import KernelSpec

    from core.agents.spec import DEFAULT_SPEC

    if CONTRATO_POR_KIND[kind] == "kernelspec/1":
        return KernelSpec().model_dump(mode="json", by_alias=True)
    modelo = {"primary": DEFAULT_SPEC["model"]["primary"]}
    return AgentSpecV3.model_validate({"model": modelo}).model_dump(mode="json", by_alias=True)


async def escritura_habilitada(tenant_id: uuid.UUID) -> bool:
    """Con el runtime en `legacy` (ADR-0112) un agente no hace nada: hasta HU-AOS-13/14 solo
    el master escribe agentes, salvo en los negocios con `aos_runtime = 'v3'`."""
    async with get_session() as session:
        valor = (
            await session.execute(text("select aos_runtime from tenants where id = :t"), {"t": tenant_id})
        ).scalar_one_or_none()
    return valor == "v3"


async def _validar(tenant_id: uuid.UUID, contenido: Any, contrato: Contrato, catalogo: Catalogo) -> dict[str, Any]:
    """Valida forma, linter y secretos. Devuelve el contenido normalizado."""
    issues = revisar_spec(contenido, catalogo, contrato)
    if issues:
        raise ErroresDeContrato(issues)
    spec, _ = validar_spec(contenido, contrato)
    assert spec is not None
    if isinstance(spec, AgentSpecV3):
        from core.agents.secretos import secretos_configurados

        configurados = await secretos_configurados(tenant_id)
        faltan: list[Issue] = []
        for i, tool in enumerate(spec.declarativeTools):
            for header, valor in tool.headers.items():
                if not isinstance(valor, SecretRef):
                    continue
                if valor.secret not in configurados:
                    faltan.append(
                        Issue(
                            "secreto_inexistente",
                            f"declarativeTools[{i}].headers.{header}",
                            f"El secreto '{valor.secret}' no está configurado en el negocio.",
                        )
                    )
                elif not set(tool.allowedDomains) <= set(configurados[valor.secret]):
                    faltan.append(
                        Issue(
                            "secreto_fuera_de_dominio",
                            f"declarativeTools[{i}].allowedDomains",
                            f"El secreto '{valor.secret}' no puede viajar a todos estos dominios.",
                        )
                    )
        if faltan:
            raise ErroresDeContrato(faltan)
    return spec.model_dump(mode="json", by_alias=True)


# ------------------------------------------------------------------ lectura
async def obtener_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> AgentRecord:
    _exigir_scope(tenant_id)
    async with get_session() as session:
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
    return agente


async def listar_agentes(tenant_id: uuid.UUID) -> list[tuple[AgentRecord, int | None]]:
    """Agentes del negocio con el número de su última versión."""
    _exigir_scope(tenant_id)
    ultima = (
        select(AgentVersionRecord.agent_id, func.max(AgentVersionRecord.version).label("v"))
        .where(AgentVersionRecord.tenant_id == tenant_id)
        .group_by(AgentVersionRecord.agent_id)
        .subquery()
    )
    async with get_session() as session:
        filas = (
            await session.execute(
                select(AgentRecord, ultima.c.v)
                .outerjoin(ultima, ultima.c.agent_id == AgentRecord.id)
                .where(AgentRecord.tenant_id == tenant_id, AgentRecord.deleted_at.is_(None))
                .order_by(AgentRecord.created_at, AgentRecord.id)
                .limit(500)
            )
        ).all()
    return [(a, v) for a, v in filas]


async def obtener_borrador(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> AgentDraftRecord:
    await obtener_agente(tenant_id, agent_id)
    async with get_session() as session:
        borrador = (
            await session.execute(
                select(AgentDraftRecord).where(
                    AgentDraftRecord.tenant_id == tenant_id,
                    AgentDraftRecord.agent_id == agent_id,
                    AgentDraftRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if borrador is None:
        raise AgenteNoEncontrado(str(agent_id))
    return borrador


async def listar_versiones(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[AgentVersionRecord]:
    await obtener_agente(tenant_id, agent_id)
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(AgentVersionRecord)
                    .where(AgentVersionRecord.tenant_id == tenant_id, AgentVersionRecord.agent_id == agent_id)
                    .order_by(AgentVersionRecord.version.desc())
                    .limit(200)
                )
            ).scalars()
        )


async def obtener_version(tenant_id: uuid.UUID, agent_id: uuid.UUID, version_id: uuid.UUID) -> AgentVersionRecord:
    await obtener_agente(tenant_id, agent_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                select(AgentVersionRecord).where(
                    AgentVersionRecord.tenant_id == tenant_id,
                    AgentVersionRecord.agent_id == agent_id,
                    AgentVersionRecord.id == version_id,
                )
            )
        ).scalar_one_or_none()
    if fila is None:
        raise AgenteNoEncontrado(str(version_id))
    return fila


# ------------------------------------------------------------------ escritura
async def crear_agente(
    tenant_id: uuid.UUID,
    *,
    key: str,
    kind: str,
    name: str,
    description: str = "",
    actor: uuid.UUID | None = None,
) -> AgentRecord:
    """Crea el agente y su borrador (con los defaults del contrato). Las keys del lector
    tolerante están reservadas para el backfill (adenda de ADR-0113)."""
    _exigir_scope(tenant_id)
    if key in KEYS_RESERVADAS:
        raise Conflicto(f"La key '{key}' está reservada para el agente que crea el sistema.")
    if kind not in CONTRATO_POR_KIND:
        raise ValueError(f"kind inválido: {kind}")
    agente = AgentRecord(
        id=uuid.uuid4(), tenant_id=tenant_id, key=key, kind=kind, name=name, description=description, created_by=actor
    )
    try:
        async with get_session() as session:
            session.add(agente)
            await session.flush()
            session.add(
                AgentDraftRecord(
                    tenant_id=tenant_id, agent_id=agente.id, content=contenido_inicial(kind), updated_by=actor
                )
            )
    except IntegrityError as exc:
        raise Conflicto(f"Ya existe un agente con la key '{key}'.") from exc
    return agente


async def actualizar_agente(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> AgentRecord:
    """Nombre, descripción y estado. `key` y `kind` no cambian (la instancia fija su rol por
    FK). Archivar un agente instanciado en una conexión → `Conflicto`."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        agente = (
            await session.execute(
                select(AgentRecord)
                .where(AgentRecord.tenant_id == tenant_id, AgentRecord.id == agent_id, AgentRecord.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if agente is None:
            raise AgenteNoEncontrado(str(agent_id))
        if status == "archived" and agente.status != "archived":
            instanciado = (
                await session.execute(
                    text(
                        "select 1 from agent_instances where tenant_id = :t and agent_id = :a "
                        "and deleted_at is null limit 1"
                    ),
                    {"t": tenant_id, "a": agent_id},
                )
            ).first()
            if instanciado is not None:
                raise Conflicto("El agente está instanciado en una conexión: quítalo de ella antes de archivarlo.")
        if name is not None:
            agente.name = name
        if description is not None:
            agente.description = description
        if status is not None:
            agente.status = status
    return agente


async def guardar_borrador(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    contenido: Any,
    lock_version: int,
    *,
    catalogo: Catalogo,
    actor: uuid.UUID | None = None,
) -> AgentDraftRecord:
    """Valida y guarda el borrador si `lock_version` es el vigente; si no, `Conflicto`."""
    agente = await obtener_agente(tenant_id, agent_id)
    if agente.status != "active":
        raise Conflicto("El agente está archivado.")
    normalizado = await _validar(tenant_id, contenido, CONTRATO_POR_KIND[agente.kind], catalogo)
    async with get_session() as session:
        nuevo = (
            await session.execute(
                update(AgentDraftRecord)
                .where(
                    AgentDraftRecord.tenant_id == tenant_id,
                    AgentDraftRecord.agent_id == agent_id,
                    AgentDraftRecord.lock_version == lock_version,
                    AgentDraftRecord.deleted_at.is_(None),
                )
                .values(
                    content=normalizado,
                    lock_version=AgentDraftRecord.lock_version + 1,
                    updated_by=actor,
                    updated_at=func.now(),
                )
                .returning(AgentDraftRecord.lock_version)
            )
        ).scalar_one_or_none()
    if nuevo is None:
        actual = await obtener_borrador(tenant_id, agent_id)
        raise Conflicto("Otra persona guardó este borrador antes: recárgalo.", actual.lock_version)
    return await obtener_borrador(tenant_id, agent_id)


async def publicar(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    catalogo: Catalogo,
    actor: uuid.UUID | None = None,
    notes: str = "",
    label: str | None = None,
    contenido: Any = None,
    legacy_agent_spec_id: uuid.UUID | None = None,
) -> tuple[AgentVersionRecord, bool]:
    """Congela el borrador (o `contenido`, para el backfill) en una versión inmutable.
    Devuelve `(versión, creada)`; con el mismo hash que una existente, `creada = False`."""
    agente = await obtener_agente(tenant_id, agent_id)
    if agente.status != "active":
        raise Conflicto("El agente está archivado.")
    if contenido is None:
        contenido = (await obtener_borrador(tenant_id, agent_id)).content
    contrato = CONTRATO_POR_KIND[agente.kind]
    normalizado = await _validar(tenant_id, contenido, contrato, catalogo)
    huella = content_hash(normalizado)
    async with get_session() as session:
        # Serializa las publicaciones del agente: el número es max + 1.
        await session.execute(
            select(AgentRecord.id)
            .where(AgentRecord.tenant_id == tenant_id, AgentRecord.id == agent_id)
            .with_for_update()
        )
        existente = (
            await session.execute(
                select(AgentVersionRecord).where(
                    AgentVersionRecord.tenant_id == tenant_id,
                    AgentVersionRecord.agent_id == agent_id,
                    AgentVersionRecord.content_hash == huella,
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            return existente, False
        ultimo = (
            await session.execute(
                select(func.max(AgentVersionRecord.version)).where(
                    AgentVersionRecord.tenant_id == tenant_id, AgentVersionRecord.agent_id == agent_id
                )
            )
        ).scalar_one()
        numero = (ultimo or 0) + 1
        version = AgentVersionRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            agent_id=agent_id,
            version=numero,
            label=(label or f"v{numero}")[:60],
            contract=contrato,
            content=normalizado,
            content_hash=huella,
            legacy_agent_spec_id=legacy_agent_spec_id,
            notes=notes[:2000],
            created_by=actor,
        )
        session.add(version)
        await session.flush()
        await session.execute(
            update(AgentDraftRecord)
            .where(AgentDraftRecord.tenant_id == tenant_id, AgentDraftRecord.agent_id == agent_id)
            .values(base_version_id=version.id)
        )
    return version, True


__all__ = [
    "CONTRATO_POR_KIND",
    "KINDS",
    "AgenteNoEncontrado",
    "Conflicto",
    "ErroresDeContrato",
    "actualizar_agente",
    "contenido_inicial",
    "content_hash",
    "crear_agente",
    "escritura_habilitada",
    "guardar_borrador",
    "listar_agentes",
    "listar_versiones",
    "obtener_agente",
    "obtener_borrador",
    "obtener_version",
    "publicar",
]
