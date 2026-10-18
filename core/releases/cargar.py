"""Lectura de una release congelada con caché en proceso (ADR-0117 §7, HU-AOS-14).

Las releases son inmutables (`prevent_mutation()` en `connection_releases`, `release_components`
y `agent_versions`), así que lo cargado de una release se guarda en proceso por
`(tenant_id, release_id)` y nunca se invalida. El tope es por BYTES del manifiesto (los
overrides de playbooks llevan texto), no por cantidad: con 300 negocios × conexiones ×
(activa + las sticky de un episodio) un tope por cantidad se vaciaría solo. Al pasarse, se
descarta la menos usada. Un deploy la vacía (vive en el proceso).

Los procedimientos abiertos (flujo del core y de los packs del binario + las secciones del
negocio congeladas) se derivan al pedirlos por primera vez y quedan en la misma entrada.
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
from core.shared.logger import get_logger
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease

if TYPE_CHECKING:
    from agentcore_contracts import Procedure

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
    (2) si no, el especialista cuya key es `routing.defaultAgent` del KernelSpec;
    (3) si no, el único especialista. Sin especialista (o con varios y ninguno por defecto):
    `ErrorDeRelease`. El Coordinador nunca atiende (ADR-0112 §1)."""
    especialistas = [a for a in agentes if a.role == "specialist"]
    if owner_instance_id is not None:
        dueno = next((a for a in especialistas if a.instance_id == owner_instance_id), None)
        if dueno is not None:
            return dueno
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
    # los `components` del manifiesto: nunca desde las instancias o agentes vivos.
    componentes: tuple[AgenteCongelado, ...] = ()
    _procedimientos: dict[str, Procedure] | None = field(default=None, repr=False)

    @property
    def skills(self) -> list[str]:
        return [s["pack_id"] for s in self.manifest.get("skills", [])]

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

    @property
    def tenant_plugins(self) -> list[str]:
        return [str(p) for p in self.manifest.get("tenant_plugins", [])]

    def foco(self, owner_instance_id: uuid.UUID | None = None) -> AgenteCongelado:
        kernel = self.kernel
        return elegir_foco(self.componentes, kernel.content if kernel is not None else None, owner_instance_id)

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
        )

    def procedimientos(self) -> dict[str, Procedure]:
        """Procedimientos de la release por id (hoy, los abiertos del flujo; ADR-0113 §5)."""
        if self._procedimientos is None:
            self._procedimientos = derivar_procedimientos(self.skills, self.flow_tenant)
        return self._procedimientos


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
    _bytes_en_cache = 0


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
    componentes, bytes_agentes = await _componentes(session, tenant_id, manifest)
    release = ReleaseCongelada(
        id=fila[0],
        tenant_id=tenant_id,
        connection_id=fila[1],
        number=fila[2],
        manifest_hash=fila[3],
        manifest=manifest,
        idle_minutes=int(fila[5]) if fila[5] else DEFAULT_IDLE_MINUTES,
        bytes=len(texto.encode("utf-8")) + bytes_agentes,
        componentes=componentes,
    )
    _guardar(release)
    return release


async def _componentes(
    session: AsyncSession, tenant_id: uuid.UUID, manifest: Mapping[str, Any]
) -> tuple[tuple[AgenteCongelado, ...], int]:
    """Las versiones de los `components` del manifiesto, por su `agent_version_id` (inmutables;
    `release_components` las ata al negocio y a la conexión). Un componente cuya versión no
    está (imposible por la FK) se omite con un log de error."""
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
