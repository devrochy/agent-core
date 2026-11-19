"""Grafo de permisos A2A versionado por negocio (ADR-0116 §2, adenda HU-AOS-18).

Quién puede pasar la conversación (`handoff`), encargar (`delegate`) o consultar (`query`) a quién.
Un grafo vivo por negocio (`a2a_graphs`); cada publicación es una versión inmutable
(`a2a_graph_versions` + `agent_links`, append-only): editar = versión nueva. La release congela la
última versión (`a2a_graph` en el manifiesto) y la guarda (`core/kernel/a2a.py`) lee esa, nunca las
tablas vivas.

Publicar es idempotente por `content_hash` y corre bajo un lock consultivo por negocio: la primera
versión no tiene fila que bloquear y dos altas concurrentes chocarían con `uq_a2a_graphs_tenant`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

# Broadcast y colaboración son internos del Coordinador: no van en un enlace (ADR-0116 §1).
MODOS = ("delegate", "handoff", "query")
MODOS_INTERNOS = ("broadcast", "collaborate")
CLASES_DE_DATOS = ("public", "internal", "pii", "sensitive")
_NOMBRE_DEL_GRAFO = "principal"
# Lock consultivo (clave, negocio), como la cuota de `billing/enforcement.py`.
_LOCK = "a2a_grafo"


class ErrorDelGrafo(ValueError):
    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.codigo = codigo


class EnlaceA2A(BaseModel):
    """Un permiso `from → to`. Los límites solo bajan los del KernelSpec (lo revisa el build)."""

    model_config = ConfigDict(extra="forbid")

    from_agent_id: uuid.UUID
    to_agent_id: uuid.UUID
    modes: list[str] = Field(min_length=1, max_length=3)
    intents: list[str] = Field(default=[], max_length=20)
    max_depth: int = Field(default=3, ge=1, le=5)
    timeout_ms: int = Field(default=15_000, ge=500, le=60_000)
    budget_usd: float = Field(default=0.02, ge=0, le=10)
    budget_tokens: int = Field(default=20_000, ge=0, le=1_000_000)
    data_classes: list[str] = Field(default=["public", "internal"], max_length=4)

    @field_validator("modes")
    @classmethod
    def _modos(cls, valor: list[str]) -> list[str]:
        for modo in valor:
            if modo in MODOS_INTERNOS:
                raise ValueError(f"modo_no_permitido: '{modo}' es interno del Coordinador")
            if modo not in MODOS:
                raise ValueError(f"modo_no_permitido: '{modo[:20]}'")
        return sorted(set(valor))

    @field_validator("intents")
    @classmethod
    def _intenciones(cls, valor: list[str]) -> list[str]:
        for intencion in valor:
            if not intencion or len(intencion) > 80:
                raise ValueError("intención vacía o de más de 80 caracteres")
        return sorted(set(valor))

    @field_validator("data_classes")
    @classmethod
    def _clases(cls, valor: list[str]) -> list[str]:
        for clase in valor:
            if clase not in CLASES_DE_DATOS:
                raise ValueError(f"clase de datos desconocida: '{clase[:20]}'")
        return sorted(set(valor))

    @model_validator(mode="after")
    def _sin_autollamada(self) -> EnlaceA2A:
        if self.from_agent_id == self.to_agent_id:
            raise ValueError("autollamada: un agente no se llama a sí mismo")
        return self

    def canonico(self) -> dict[str, Any]:
        datos = self.model_dump(mode="json")
        # El numeric(10,6) de la base: el hash no cambia por cómo se escribió el número.
        datos["budget_usd"] = f"{self.budget_usd:.6f}"
        return datos


@dataclass(frozen=True)
class VersionDelGrafo:
    id: uuid.UUID
    version: int
    content_hash: str
    created_at: datetime
    created_by: uuid.UUID | None
    enlaces: tuple[EnlaceA2A, ...]


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("grafo A2A: fuera del tenant_scope del negocio")


def codigo_de_error(exc: Exception) -> str:
    """El código de la API para un error de validación de enlaces (`modo_no_permitido`, …)."""
    texto = str(exc)
    for codigo in ("modo_no_permitido", "autollamada"):
        if codigo in texto:
            return codigo
    return "enlace_invalido"


def normalizar(enlaces: Iterable[EnlaceA2A | Mapping[str, Any]]) -> list[EnlaceA2A]:
    """Valida y ordena por `(from, to)`; un par repetido es error (un solo enlace por par)."""
    lista = [e if isinstance(e, EnlaceA2A) else EnlaceA2A.model_validate(e) for e in enlaces]
    lista.sort(key=lambda e: (str(e.from_agent_id), str(e.to_agent_id)))
    pares = [(e.from_agent_id, e.to_agent_id) for e in lista]
    if len(pares) != len(set(pares)):
        raise ErrorDelGrafo("enlace_repetido", "Hay dos permisos para el mismo par de agentes.")
    if len(lista) > 200:
        raise ErrorDelGrafo("demasiados_enlaces", "Un grafo admite hasta 200 permisos.")
    return lista


def hash_de(enlaces: Iterable[EnlaceA2A]) -> str:
    canonico = json.dumps([e.canonico() for e in enlaces], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonico.encode()).hexdigest()


def advertencias(enlaces: Iterable[EnlaceA2A]) -> list[str]:
    """Ciclos de `handoff` del grafo (A→B→…→A). No bloquean (devolver el control es legítimo):
    el panel los muestra antes de guardar (HU-AOS-26) y la guarda los corta en runtime."""
    aristas: dict[str, set[str]] = {}
    for e in enlaces:
        if "handoff" in e.modes:
            aristas.setdefault(str(e.from_agent_id), set()).add(str(e.to_agent_id))
    ciclos: set[tuple[str, ...]] = set()

    def visitar(nodo: str, camino: list[str]) -> None:
        for siguiente in sorted(aristas.get(nodo, ())):
            if siguiente in camino:
                ciclo = camino[camino.index(siguiente) :]
                giro = ciclo.index(min(ciclo))
                ciclos.add(tuple(ciclo[giro:] + ciclo[:giro]))
            elif len(camino) < 6:
                visitar(siguiente, [*camino, siguiente])

    for inicio in sorted(aristas):
        visitar(inicio, [inicio])
    return [f"ciclo_handoff:{'>'.join(c)}" for c in sorted(ciclos)]


async def _enlaces_de(session: AsyncSession, tenant_id: uuid.UUID, version_id: uuid.UUID) -> tuple[EnlaceA2A, ...]:
    filas = (
        await session.execute(
            text(
                "SELECT from_agent_id, to_agent_id, modes, intents, max_depth, timeout_ms, budget_usd, "
                "budget_tokens, data_classes FROM agent_links WHERE tenant_id = :t AND graph_version_id = :v "
                "ORDER BY from_agent_id::text, to_agent_id::text"
            ),
            {"t": tenant_id, "v": version_id},
        )
    ).mappings()
    return tuple(
        EnlaceA2A(
            from_agent_id=f["from_agent_id"],
            to_agent_id=f["to_agent_id"],
            modes=list(f["modes"]),
            intents=list(f["intents"]),
            max_depth=f["max_depth"],
            timeout_ms=f["timeout_ms"],
            budget_usd=float(f["budget_usd"]),
            budget_tokens=f["budget_tokens"],
            data_classes=list(f["data_classes"]),
        )
        for f in filas
    )


async def _ultima(session: AsyncSession, tenant_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "SELECT v.id, v.version, v.content_hash, v.created_at, v.created_by "
                "FROM a2a_graph_versions v JOIN a2a_graphs g ON g.tenant_id = v.tenant_id AND g.id = v.graph_id "
                "WHERE v.tenant_id = :t AND g.deleted_at IS NULL ORDER BY v.version DESC LIMIT 1"
            ),
            {"t": tenant_id},
        )
    ).first()


async def vigente(tenant_id: uuid.UUID, session: AsyncSession | None = None) -> VersionDelGrafo | None:
    """La última versión publicada (la que congelará la próxima release), o None sin grafo."""
    _exigir_scope(tenant_id)
    if session is None:
        async with get_session() as propia:
            return await vigente(tenant_id, propia)
    fila = await _ultima(session, tenant_id)
    if fila is None:
        return None
    return VersionDelGrafo(fila[0], fila[1], fila[2], fila[3], fila[4], await _enlaces_de(session, tenant_id, fila[0]))


async def versiones(tenant_id: uuid.UUID, *, limite: int = 50) -> list[VersionDelGrafo]:
    """Historial (sin enlaces), de la más nueva a la más vieja."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT v.id, v.version, v.content_hash, v.created_at, v.created_by "
                    "FROM a2a_graph_versions v JOIN a2a_graphs g ON g.tenant_id = v.tenant_id AND g.id = v.graph_id "
                    "WHERE v.tenant_id = :t AND g.deleted_at IS NULL ORDER BY v.version DESC LIMIT :n"
                ),
                {"t": tenant_id, "n": limite},
            )
        ).all()
    return [VersionDelGrafo(f[0], f[1], f[2], f[3], f[4], ()) for f in filas]


async def _agentes_del_negocio(session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    filas = (
        await session.execute(
            text("SELECT id FROM agents WHERE tenant_id = :t AND id = ANY(:ids) AND deleted_at IS NULL"),
            {"t": tenant_id, "ids": list(ids)},
        )
    ).all()
    return {f[0] for f in filas}


async def publicar(
    tenant_id: uuid.UUID,
    enlaces: Iterable[EnlaceA2A | Mapping[str, Any]],
    *,
    created_by: uuid.UUID | None = None,
) -> tuple[VersionDelGrafo, bool]:
    """Publica una versión del grafo. Devuelve `(versión, creada)`; con el mismo contenido que la
    última, esa con `creada = False`. Aplica con la próxima release de cada conexión."""
    _exigir_scope(tenant_id)
    lista = normalizar(enlaces)
    huella = hash_de(lista)
    async with get_session() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k), hashtext(:t))"), {"k": _LOCK, "t": str(tenant_id)}
        )
        ultima = await _ultima(session, tenant_id)
        if ultima is not None and ultima[2] == huella:
            return VersionDelGrafo(ultima[0], ultima[1], ultima[2], ultima[3], ultima[4], tuple(lista)), False
        ids = {e.from_agent_id for e in lista} | {e.to_agent_id for e in lista}
        faltan = ids - await _agentes_del_negocio(session, tenant_id, ids)
        if faltan:
            raise ErrorDelGrafo("agente_desconocido", "Un permiso nombra un agente que no es de este negocio.")
        grafo = (
            await session.execute(
                text("SELECT id FROM a2a_graphs WHERE tenant_id = :t AND deleted_at IS NULL"), {"t": tenant_id}
            )
        ).scalar_one_or_none()
        if grafo is None:
            grafo = uuid.uuid4()
            await session.execute(
                text("INSERT INTO a2a_graphs (id, tenant_id, name) VALUES (:id, :t, :n)"),
                {"id": grafo, "t": tenant_id, "n": _NOMBRE_DEL_GRAFO},
            )
        numero = (ultima[1] if ultima is not None else 0) + 1
        version_id = uuid.uuid4()
        creada = (
            await session.execute(
                text(
                    "INSERT INTO a2a_graph_versions (id, tenant_id, graph_id, version, content_hash, created_by) "
                    "VALUES (:id, :t, :g, :n, :h, :by) RETURNING created_at"
                ),
                {"id": version_id, "t": tenant_id, "g": grafo, "n": numero, "h": huella, "by": created_by},
            )
        ).scalar_one()
        for e in lista:
            await session.execute(
                text(
                    "INSERT INTO agent_links (tenant_id, graph_version_id, from_agent_id, to_agent_id, modes, "
                    "intents, max_depth, timeout_ms, budget_usd, budget_tokens, data_classes) VALUES "
                    "(:t, :v, :f, :to, :modes, :intents, :d, :ms, :usd, :tok, :dc)"
                ),
                {
                    "t": tenant_id,
                    "v": version_id,
                    "f": e.from_agent_id,
                    "to": e.to_agent_id,
                    "modes": e.modes,
                    "intents": e.intents,
                    "d": e.max_depth,
                    "ms": e.timeout_ms,
                    "usd": e.budget_usd,
                    "tok": e.budget_tokens,
                    "dc": e.data_classes,
                },
            )
        await session.execute(
            text("UPDATE a2a_graphs SET updated_at = now() WHERE tenant_id = :t AND id = :g"),
            {"t": tenant_id, "g": grafo},
        )
    return VersionDelGrafo(version_id, numero, huella, creada, created_by, tuple(lista)), True


def para_el_manifiesto(version: VersionDelGrafo) -> dict[str, Any]:
    """La copia congelada que va en `a2a_graph` del manifiesto (entra al hash)."""
    return {
        "version_id": str(version.id),
        "version": version.version,
        "content_hash": version.content_hash,
        "links": [e.canonico() for e in version.enlaces],
    }


Modo = Literal["delegate", "handoff", "query"]

__all__ = [
    "CLASES_DE_DATOS",
    "MODOS",
    "EnlaceA2A",
    "ErrorDelGrafo",
    "Modo",
    "VersionDelGrafo",
    "advertencias",
    "codigo_de_error",
    "hash_de",
    "normalizar",
    "para_el_manifiesto",
    "publicar",
    "versiones",
    "vigente",
]
