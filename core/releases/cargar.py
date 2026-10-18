"""Lectura de una release congelada con caché en proceso (ADR-0117 §7, HU-AOS-14).

Las releases son inmutables (`prevent_mutation()` en `connection_releases`, `release_components`
y `agent_versions`), así que lo cargado de una release se guarda en proceso por
`(tenant_id, release_id)` y nunca se invalida. El tope es por BYTES del manifiesto (los
overrides de playbooks llevan texto), no por cantidad: con 300 negocios × conexiones ×
(activa + las sticky de un episodio) un tope por cantidad se vaciaría solo. Al pasarse, se
descarta la menos usada. Un deploy la vacía (vive en el proceso).

Los procedimientos abiertos (flujo del core y de los packs del binario + las secciones del
negocio congeladas) se derivan al pedirlos por primera vez y quedan en la misma entrada.

HU-AOS-16 (adenda de ADR-0118): la release también lee el **contenido congelado** de sus
habilidades (los `procedure/1` del manifiesto de `pack_versions` de cada `(pack_id, version)`,
validados; caché global en proceso por versión, no por release: son inmutables) y el `content`
de las versiones de sus agentes. Con `release/2`, cada agente trae sus procedimientos RESUELTOS
(`components[].procedures`); con `release/1` no hay procedimientos por agente y rige la regla
transitoria de HU-AOS-05 (los del binario de los packs habilitados).
"""

from __future__ import annotations

import copy
import json
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.kernel.episodio import DEFAULT_IDLE_MINUTES
from core.releases.manifiesto import SCHEMA_SIN_PROCEDIMIENTOS
from core.releases.procedimientos import OPEN_VERSION, major, ofrecible
from core.shared.logger import get_logger
from core.shared.release_vigente import ArtefactosDeRelease

if TYPE_CHECKING:
    from agentcore_contracts import Procedure

log = get_logger(__name__)

# 64 MiB: holgado para 300 negocios con overrides de 8 KiB.
MAX_BYTES = 64 * 1024 * 1024
KEY_GENERAL = "general"


@dataclass(frozen=True)
class AgenteDeRelease:
    """Un componente de la release con el contenido congelado de su versión."""

    instance_id: str
    agent_id: str
    key: str
    role: str
    is_default: bool
    version_id: str
    contract: str
    content: Mapping[str, Any]
    # `(id, versión exacta)` resueltos en el build (`release/2`); None en `release/1`.
    procedimientos: tuple[tuple[str, str], ...] | None

    @property
    def tools(self) -> frozenset[str]:
        """Acciones que la versión declara en `tools` (habilitadas): el consentimiento del
        agente (ADR-0118 §3). Vacío para el Coordinador."""
        return frozenset(
            str(t.get("tool"))
            for t in self.content.get("tools") or []
            if isinstance(t, Mapping) and t.get("enabled", True) and isinstance(t.get("tool"), str)
        )

    @property
    def declarativas(self) -> frozenset[str]:
        return frozenset(
            str(t.get("name"))
            for t in self.content.get("declarativeTools") or []
            if isinstance(t, Mapping) and isinstance(t.get("name"), str)
        )


@dataclass
class ReleaseCongelada:
    id: uuid.UUID
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    number: int
    manifest_hash: str
    manifest: Mapping[str, Any]
    idle_minutes: int
    bytes: int
    # instance_id → agente, con el contenido de su versión.
    agentes: dict[str, AgenteDeRelease] = field(default_factory=dict)
    # `(id, version)` → procedimiento publicado por una habilidad congelada de la release.
    congelados: dict[tuple[str, str], Procedure] = field(default_factory=dict, repr=False)
    _procedimientos: dict[str, Procedure] | None = field(default=None, repr=False)

    @property
    def skills(self) -> list[str]:
        return [s["pack_id"] for s in self.manifest.get("skills", [])]

    @property
    def tenant_plugins(self) -> list[str]:
        return [str(p) for p in self.manifest.get("tenant_plugins", [])]

    @property
    def packs(self) -> list[str]:
        """Habilidades de la release: las de la plataforma más los plugins del negocio."""
        return [*self.skills, *self.tenant_plugins]

    @property
    def con_procedimientos_por_agente(self) -> bool:
        """`release/2`: los componentes traen sus procedimientos resueltos."""
        return self.manifest.get("schema") != SCHEMA_SIN_PROCEDIMIENTOS

    @property
    def app_settings(self) -> dict[str, str | None]:
        return dict(self.manifest.get("app_settings") or {})

    @property
    def playbooks(self) -> dict[str, str]:
        return {p["name"]: p["text"] for p in self.manifest.get("playbook_overrides", [])}

    @property
    def flow_tenant(self) -> Any:
        flujo = self.manifest.get("flow_tenant")
        return flujo.get("sections") if isinstance(flujo, Mapping) else None

    def artefactos(self, lane: str = "stable", agente: str | None = None) -> ArtefactosDeRelease:
        """Lo que el turno lee de la release. `agente`: el `instance_id` en foco; por defecto el
        agente por defecto de la release."""
        en_foco = agente if agente in self.agentes else self.agente_por_defecto()
        return ArtefactosDeRelease(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            release_id=self.id,
            lane=lane,
            idle_minutes=self.idle_minutes,
            app_settings=self.app_settings,
            playbooks=self.playbooks,
            # Copia: lo de la caché es de todos los turnos de la release y nadie debe mutarlo.
            flow_tenant=copy.deepcopy(self.flow_tenant),
            skills=tuple(self.packs),
            procedimientos=self.procedimientos(),
            agentes=self.agentes,
            procedimientos_por_agente=(
                {iid: self.procedimientos_de(iid) for iid in self.agentes}
                if self.con_procedimientos_por_agente
                else None
            ),
            agente_en_foco=en_foco,
        )

    def procedimientos(self) -> dict[str, Procedure]:
        """TODOS los procedimientos de la release por id: los abiertos del flujo (ADR-0113 §5,
        `0.0.0`) y los publicados por sus habilidades congeladas que son ofrecibles (su
        habilidad y su `requires` en la release). Sirve a la revalidación y a las puertas; los
        candidatos de un turno salen de `procedimientos_de`."""
        if self._procedimientos is None:
            salida = derivar_procedimientos(self.skills, self.flow_tenant)
            packs = set(self.packs)
            for (pid, _version), proc in sorted(self.congelados.items()):
                if ofrecible(proc, packs):
                    salida[pid] = proc
            self._procedimientos = salida
        return self._procedimientos

    def procedimientos_de(self, instance_id: str | None) -> dict[str, Procedure]:
        """Los procedimientos PUBLICADOS del agente (sin los abiertos), por id: lo que su
        versión declara, resuelto en el build. Vacío si la instancia no es de la release o la
        release es `release/1`."""
        agente = self.agentes.get(instance_id) if instance_id is not None else None
        if agente is None or agente.procedimientos is None:
            return {}
        packs = set(self.packs)
        salida: dict[str, Procedure] = {}
        for pid, version in agente.procedimientos:
            proc = self.congelados.get((pid, version))
            if proc is not None and version != OPEN_VERSION and ofrecible(proc, packs):
                salida[pid] = proc
        return salida

    def procedimiento_para(self, procedure_id: str, version: str) -> Procedure | None:
        """El procedimiento de la release con el que sigue una tarea abierta en `version`: el
        mismo id y el mismo major (ADR-0117 §4, HU-AOS-16); None si desapareció o cambió de
        major (la tarea se cancela o se escala)."""
        proc = self.procedimientos().get(procedure_id)
        if proc is None or major(proc.version) != major(version):
            return None
        return proc

    def agente_por_defecto(self) -> str | None:
        """`instance_id` del agente por defecto de la release (`is_default`, `release/2`); en
        una `release/1`, el General si está, si no el primer especialista."""
        especialistas = [a for a in self.agentes.values() if a.role != "core"]
        if not especialistas:
            return None
        for a in especialistas:
            if a.is_default:
                return a.instance_id
        for a in especialistas:
            if a.key == KEY_GENERAL:
                return a.instance_id
        return especialistas[0].instance_id


def derivar_procedimientos(skills: list[str], flow_tenant: Any) -> dict[str, Procedure]:
    """Procedimientos abiertos del flujo compuesto (core y packs del binario, más las secciones
    del negocio), sin los filtros del turno: es un superconjunto de los que referencia el
    General (el lector tolerante los resuelve contra las acciones candidatas)."""
    from core.agents.flow import (
        all_pack_ids,
        compose_flow,
        flow_to_procedures,
        load_core_flow,
        load_pack_flows,
        parse_sections,
    )
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    secciones = compose_flow(
        load_core_flow(),
        load_pack_flows(sorted(skills)),
        parse_sections(flow_tenant) if flow_tenant is not None else [],
        all_defaults=load_pack_flows(all_pack_ids()),
    )
    catalogo = catalogo_de_acciones(plugin_manager.list_modules())
    return {p.id: p for p in flow_to_procedures(secciones, catalogo=catalogo)}


_cache: OrderedDict[tuple[uuid.UUID, uuid.UUID], ReleaseCongelada] = OrderedDict()
_bytes_en_cache = 0
# Procedimientos publicados por versión de habilidad: `pack_versions` es inmutable (salvo
# `yanked`), así que se cachean por `(pack_id, version)` para todo el proceso, no por release.
_congelados_por_version: dict[tuple[str, str], tuple[Procedure, ...]] = {}


def _guardar(release: ReleaseCongelada) -> None:
    global _bytes_en_cache
    clave = (release.tenant_id, release.id)
    if clave in _cache:
        return
    _cache[clave] = release
    _bytes_en_cache += release.bytes
    while _bytes_en_cache > MAX_BYTES and len(_cache) > 1:
        _, vieja = _cache.popitem(last=False)
        _bytes_en_cache -= vieja.bytes


def vaciar_cache() -> None:
    """Solo para tests."""
    global _bytes_en_cache
    _cache.clear()
    _congelados_por_version.clear()
    _bytes_en_cache = 0


def _procedimientos_del_manifiesto(pack_id: str, version: str, texto: str) -> tuple[Procedure, ...]:
    """Los `procedure/1` válidos del manifiesto registrado de una versión del pack."""
    from core.kernel.procedures import validar

    try:
        crudos = json.loads(texto).get("provides", {}).get("procedures", [])
    except (ValueError, AttributeError):
        log.error("release_pack_manifest_invalido", pack_id=pack_id, version=version)
        return ()
    salida: list[Procedure] = []
    for crudo in crudos if isinstance(crudos, list) else []:
        proc, _problemas = validar(crudo)
        if proc is None:
            log.warning("release_pack_procedimiento_invalido", pack_id=pack_id, version=version)
            continue
        salida.append(proc)
    return tuple(salida)


async def _congelados(session: AsyncSession, manifest: Mapping[str, Any]) -> tuple[dict[tuple[str, str], Procedure], int]:
    """Procedimientos publicados por las habilidades de la release, desde `pack_versions`
    (tabla de sistema: `app_runtime` tiene SELECT). Devuelve también los bytes leídos."""
    salida: dict[tuple[str, str], Procedure] = {}
    leidos = 0
    for skill in manifest.get("skills", []):
        pack_id, version = str(skill.get("pack_id")), str(skill.get("version"))
        clave = (pack_id, version)
        if clave not in _congelados_por_version:
            fila = (
                await session.execute(
                    text("SELECT manifest::text FROM pack_versions WHERE pack_id = :p AND version = :v"),
                    {"p": pack_id, "v": version},
                )
            ).first()
            if fila is None:
                log.error("release_pack_version_missing", pack_id=pack_id, version=version)
                _congelados_por_version[clave] = ()
            else:
                leidos += len(fila[0].encode("utf-8"))
                _congelados_por_version[clave] = _procedimientos_del_manifiesto(pack_id, version, fila[0])
        for proc in _congelados_por_version[clave]:
            salida[(proc.id, proc.version)] = proc
    return salida, leidos


async def _agentes(
    session: AsyncSession, tenant_id: uuid.UUID, manifest: Mapping[str, Any]
) -> tuple[dict[str, AgenteDeRelease], int]:
    """Los componentes con el `content` congelado de su versión (filtrado por `tenant_id` además
    de RLS). Devuelve también los bytes leídos."""
    componentes = [c for c in manifest.get("components", []) if isinstance(c, Mapping)]
    ids = [uuid.UUID(str(c["agent_version_id"])) for c in componentes if c.get("agent_version_id")]
    contenidos: dict[str, Mapping[str, Any]] = {}
    leidos = 0
    if ids:
        filas = (
            await session.execute(
                text("SELECT id, content::text FROM agent_versions WHERE tenant_id = :t AND id = ANY(:ids)"),
                {"t": tenant_id, "ids": ids},
            )
        ).all()
        for vid, texto in filas:
            leidos += len(texto.encode("utf-8"))
            contenidos[str(vid)] = json.loads(texto)
    con_procedimientos = manifest.get("schema") != SCHEMA_SIN_PROCEDIMIENTOS
    salida: dict[str, AgenteDeRelease] = {}
    for c in componentes:
        vid = str(c.get("agent_version_id"))
        if vid not in contenidos:
            log.error("release_agent_version_missing", agent_version_id=vid)
            continue
        resueltos = (
            tuple(
                (str(p["id"]), str(p["version"]))
                for p in c.get("procedures", [])
                if isinstance(p, Mapping) and p.get("id") and p.get("version")
            )
            if con_procedimientos
            else None
        )
        iid = str(c.get("instance_id"))
        salida[iid] = AgenteDeRelease(
            instance_id=iid,
            agent_id=str(c.get("agent_id")),
            key=str(c.get("agent_key")),
            role=str(c.get("role")),
            is_default=bool(c.get("is_default", False)),
            version_id=vid,
            contract=str(c.get("contract")),
            content=contenidos[vid],
            procedimientos=resueltos,
        )
    return salida, leidos


async def cargar_release(
    session: AsyncSession, tenant_id: uuid.UUID, release_id: uuid.UUID
) -> ReleaseCongelada | None:
    """La release del negocio (None si no existe en él). Filtra por `tenant_id` además de RLS.

    Exige el `tenant_scope` de `tenant_id` también al leer de la caché: la caché no pasa por RLS."""
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("releases: fuera del tenant_scope del negocio")
    clave = (tenant_id, release_id)
    guardada = _cache.get(clave)
    if guardada is not None:
        _cache.move_to_end(clave)
        return guardada
    fila = (
        await session.execute(
            text(
                "SELECT r.id, r.connection_id, r.number, r.manifest_hash, r.manifest::text, "
                "  (SELECT (av.content->'episode'->>'idleMinutes')::int "
                "     FROM release_components rc "
                "     JOIN agent_instances ai ON ai.tenant_id = rc.tenant_id AND ai.id = rc.agent_instance_id "
                "     JOIN agent_versions av ON av.tenant_id = rc.tenant_id AND av.id = rc.agent_version_id "
                "    WHERE rc.tenant_id = r.tenant_id AND rc.release_id = r.id AND ai.role = 'core' "
                "    LIMIT 1) "
                "FROM connection_releases r WHERE r.tenant_id = :t AND r.id = :r"
            ),
            {"t": tenant_id, "r": release_id},
        )
    ).first()
    if fila is None:
        return None
    texto: str = fila[4]
    manifest = json.loads(texto)
    congelados, bytes_packs = await _congelados(session, manifest)
    agentes, bytes_agentes = await _agentes(session, tenant_id, manifest)
    release = ReleaseCongelada(
        id=fila[0],
        tenant_id=tenant_id,
        connection_id=fila[1],
        number=fila[2],
        manifest_hash=fila[3],
        manifest=manifest,
        idle_minutes=int(fila[5]) if fila[5] else DEFAULT_IDLE_MINUTES,
        bytes=len(texto.encode("utf-8")) + bytes_packs + bytes_agentes,
        agentes=agentes,
        congelados=congelados,
    )
    _guardar(release)
    return release


__all__ = [
    "MAX_BYTES",
    "AgenteDeRelease",
    "ReleaseCongelada",
    "cargar_release",
    "derivar_procedimientos",
    "vaciar_cache",
]
