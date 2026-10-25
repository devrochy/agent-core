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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.kernel.episodio import DEFAULT_IDLE_MINUTES
from core.releases.manifiesto import SCHEMA_SIN_PROCEDIMIENTOS
from core.releases.procedimientos import OPEN_VERSION, major, ofrecible
from core.shared.logger import get_logger
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease

if TYPE_CHECKING:
    from agentcore_contracts import Procedure
    from agentcore_contracts.kernelspec import Routing

log = get_logger(__name__)

# 64 MiB: holgado para 300 negocios con overrides de 8 KiB (y las versiones de sus agentes,
# HU-AOS-15: el General más grande de hoy pesa unos 20 KiB).
MAX_BYTES = 64 * 1024 * 1024


class ErrorDeRelease(RuntimeError):
    """La release no se puede ejecutar (p. ej. sin especialista en foco). Fail-closed con
    `aos_runtime = 'v3'`: sube por el camino de error del turno."""


def elegir_foco(
    agentes: Iterable[AgenteCongelado], kernel: Mapping[str, Any] | None, owner_instance_id: uuid.UUID | None
) -> AgenteCongelado:
    """El especialista que atiende el turno (HU-AOS-15; el router llega con HU-AOS-17):
    (1) el dueño vigente de la conversación si es un especialista de ESTA release;
    (2) si no, el especialista congelado como `is_default` de la conexión (HU-AOS-16; solo
        `release/2` lo trae: en una `release/1` todos vienen en False y el paso no aplica);
    (3) si no, el especialista cuya key es `routing.defaultAgent` del KernelSpec;
    (4) si no, el único especialista. Sin especialista (o con varios y ninguno por defecto):
    `ErrorDeRelease`. El Coordinador nunca atiende (ADR-0112 §1)."""
    especialistas = [a for a in agentes if a.role == "specialist"]
    if owner_instance_id is not None:
        dueno = next((a for a in especialistas if a.instance_id == owner_instance_id), None)
        if dueno is not None:
            return dueno
    de_la_conexion = next((a for a in especialistas if a.is_default), None)
    if de_la_conexion is not None:
        return de_la_conexion
    routing = kernel.get("routing") if isinstance(kernel, Mapping) else None
    por_defecto = routing.get("defaultAgent") if isinstance(routing, Mapping) else None
    elegido = next((a for a in especialistas if a.key == por_defecto), None)
    if elegido is not None:
        return elegido
    if len(especialistas) == 1:
        return especialistas[0]
    raise ErrorDeRelease(
        "sin_especialista_en_foco: la release no tiene un especialista por defecto"
        if especialistas
        else "sin_especialista: la release no tiene especialistas"
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
    # Versiones congeladas de los agentes (HU-AOS-15), cargadas por `agent_version_id` desde
    # los `components` del manifiesto: nunca desde las instancias o agentes vivos. Con
    # `release/2` traen además `is_default` y sus procedimientos resueltos (HU-AOS-16).
    componentes: tuple[AgenteCongelado, ...] = ()
    # `(id, version)` → procedimiento publicado por una habilidad congelada de la release.
    congelados: dict[tuple[str, str], Procedure] = field(default_factory=dict, repr=False)
    _procedimientos: dict[str, Procedure] | None = field(default=None, repr=False)
    # HU-AOS-17: el `routing` del KernelSpec validado y el índice de disparadores, una vez por
    # release (inmutable): no se valida ni se indexa en cada turno.
    _ruteo: Any = field(default=None, repr=False)
    _indice: Any = field(default=None, repr=False)

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

    @property
    def kernel(self) -> AgenteCongelado | None:
        return next((c for c in self.componentes if c.role == "core"), None)

    def foco(self, owner_instance_id: uuid.UUID | None = None) -> AgenteCongelado:
        kernel = self.kernel
        return elegir_foco(self.componentes, kernel.content if kernel is not None else None, owner_instance_id)

    def ruteo(self) -> Routing:
        """`routing` del KernelSpec congelado. Si no valida (una release vieja), los valores por
        defecto (sin reglas) con un log: el turno no se cae por eso (el build ya lo valida)."""
        if self._ruteo is None:
            from agentcore_contracts.kernelspec import Routing
            from pydantic import ValidationError

            kernel = self.kernel
            crudo = kernel.content.get("routing") if kernel is not None else None
            try:
                self._ruteo = Routing.model_validate(crudo if isinstance(crudo, Mapping) else {})
            except ValidationError:
                log.error("release_routing_invalido", release_number=self.number)
                self._ruteo = Routing()
        return self._ruteo

    @property
    def a2a(self) -> Mapping[str, Any] | None:
        """El grafo de permisos A2A congelado (adenda HU-AOS-18 de ADR-0116), o None: sin grafo
        no hay enlaces y la guarda lo deniega todo con `no_link`."""
        grafo = self.manifest.get("a2a_graph")
        return grafo if isinstance(grafo, Mapping) else None

    def limites_a2a(self) -> Any:
        """`a2a` del KernelSpec congelado (`A2ALimits`); los valores por defecto si no valida."""
        from agentcore_contracts.kernelspec import A2ALimits
        from pydantic import ValidationError

        kernel = self.kernel
        crudo = kernel.content.get("a2a") if kernel is not None else None
        try:
            return A2ALimits.model_validate(crudo if isinstance(crudo, Mapping) else {})
        except ValidationError:
            log.error("release_a2a_invalido", release_number=self.number)
            return A2ALimits()

    def indice_de_disparadores(self) -> tuple[Any, dict[str, str]]:
        """Índice léxico de los procedimientos publicados de los especialistas (con los ejemplos
        de `procedures[].overrides.examples` de cada agente) y la key del dueño de cada uno (el más
        específico si lo declaran varios, `router.orden_de_duenos`; el router decide en cada turno)."""
        if self._indice is None:
            from core.kernel.disparadores import Indice, exponentes_de

            procs: dict[str, Procedure] = {}
            extras: dict[str, list[str]] = {}
            duenos: dict[str, str] = {}
            from core.kernel.router import generalista, orden_de_duenos

            gen = generalista(self, self.ruteo())
            # Del más específico al menos (el General declara los de todos): el primero que
            # declara un procedimiento es el dueño que se muestra en `candidates`.
            for agente in orden_de_duenos([a for a in self.componentes if a.role == "specialist"], gen):
                for pid, proc in self.procedimientos_de(agente.instance_id).items():
                    procs.setdefault(pid, proc)
                    duenos.setdefault(pid, agente.key)
            for agente in self.componentes:
                if agente.role != "specialist":
                    continue
                for ref in agente.content.get("procedures") or []:
                    if not isinstance(ref, Mapping) or ref.get("id") not in procs:
                        continue
                    sobre = ref.get("overrides")
                    ejemplos = sobre.get("examples") if isinstance(sobre, Mapping) else None
                    if isinstance(ejemplos, list):
                        extras.setdefault(str(ref["id"]), []).extend(str(e) for e in ejemplos[:50])
            self._indice = (Indice(exponentes_de(procs, extras)), duenos)
        return self._indice

    def artefactos(self, lane: str = "stable", foco: AgenteCongelado | None = None) -> ArtefactosDeRelease:
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
            agentes=self.componentes,
            foco=foco,
            skills=tuple(self.skills),
            tenant_plugins=tuple(self.tenant_plugins),
            procedimientos=self.procedimientos(),
            procedimientos_por_agente=(
                {c.instance_id: self.procedimientos_de(c.instance_id) for c in self.componentes}
                if self.con_procedimientos_por_agente
                else None
            ),
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

    def procedimientos_de(self, instance_id: uuid.UUID | None) -> dict[str, Procedure]:
        """Los procedimientos PUBLICADOS del agente (sin los abiertos), por id: lo que su
        versión declara, resuelto en el build. Vacío si la instancia no es de la release o la
        release es `release/1`."""
        agente = next((c for c in self.componentes if c.instance_id == instance_id), None)
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
            # Copia por release: `Procedure` no es frozen y la caché por versión es de todos los
            # negocios; una mutación accidental no debe cruzar de un negocio a otro.
            salida[(proc.id, proc.version)] = proc.model_copy(deep=True)
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
    componentes, bytes_agentes = await _componentes(session, tenant_id, manifest)
    release = ReleaseCongelada(
        id=fila[0],
        tenant_id=tenant_id,
        connection_id=fila[1],
        number=fila[2],
        manifest_hash=fila[3],
        manifest=manifest,
        idle_minutes=int(fila[5]) if fila[5] else DEFAULT_IDLE_MINUTES,
        bytes=len(texto.encode("utf-8")) + bytes_packs + bytes_agentes,
        componentes=componentes,
        congelados=congelados,
    )
    _guardar(release)
    return release


async def _componentes(
    session: AsyncSession, tenant_id: uuid.UUID, manifest: Mapping[str, Any]
) -> tuple[tuple[AgenteCongelado, ...], int]:
    """Las versiones de los `components` del manifiesto, por su `agent_version_id` (inmutables;
    `release_components` las ata al negocio y a la conexión). Un componente cuya versión no
    está (imposible por la FK) se omite con un log de error. Con `release/2` cada uno lleva
    además `is_default` y sus procedimientos resueltos `(id, versión exacta)` (HU-AOS-16); con
    `release/1`, `is_default` False y `procedimientos` None."""
    crudos = [c for c in manifest.get("components", []) if isinstance(c, Mapping)]
    ids = [c["agent_version_id"] for c in crudos if c.get("agent_version_id")]
    if not ids:
        return (), 0
    filas = (
        await session.execute(
            text(
                "SELECT id, content::text FROM agent_versions "
                "WHERE tenant_id = :t AND id = ANY(cast(:ids AS uuid[]))"
            ),
            {"t": tenant_id, "ids": ids},
        )
    ).all()
    por_id = {str(f[0]): str(f[1]) for f in filas}
    con_procedimientos = manifest.get("schema") != SCHEMA_SIN_PROCEDIMIENTOS
    salida: list[AgenteCongelado] = []
    total = 0
    for c in crudos:
        contenido = por_id.get(str(c.get("agent_version_id")))
        if contenido is None:
            log.error("release_component_sin_version", agent_key=c.get("agent_key"))
            continue
        total += len(contenido.encode("utf-8"))
        salida.append(
            AgenteCongelado(
                instance_id=uuid.UUID(str(c["instance_id"])),
                agent_id=uuid.UUID(str(c["agent_id"])),
                key=str(c["agent_key"]),
                role=str(c["role"]),
                version_id=uuid.UUID(str(c["agent_version_id"])),
                version=int(c.get("version") or 0),
                contract=str(c.get("contract") or ""),
                content=json.loads(contenido),
                is_default=con_procedimientos and bool(c.get("is_default", False)),
                procedimientos=(
                    tuple(
                        (str(p["id"]), str(p["version"]))
                        for p in c.get("procedures") or []
                        if isinstance(p, Mapping) and p.get("id") and p.get("version")
                    )
                    if con_procedimientos
                    else None
                ),
            )
        )
    return tuple(salida), total


__all__ = [
    "MAX_BYTES",
    "ErrorDeRelease",
    "ReleaseCongelada",
    "cargar_release",
    "derivar_procedimientos",
    "elegir_foco",
    "vaciar_cache",
]
