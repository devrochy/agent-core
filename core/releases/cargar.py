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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.kernel.episodio import DEFAULT_IDLE_MINUTES
from core.shared.logger import get_logger
from core.shared.release_vigente import ArtefactosDeRelease

if TYPE_CHECKING:
    from agentcore_contracts import Procedure

log = get_logger(__name__)

# 64 MiB: holgado para 300 negocios con overrides de 8 KiB.
MAX_BYTES = 64 * 1024 * 1024


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

    def artefactos(self, lane: str = "stable") -> ArtefactosDeRelease:
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
    release = ReleaseCongelada(
        id=fila[0],
        tenant_id=tenant_id,
        connection_id=fila[1],
        number=fila[2],
        manifest_hash=fila[3],
        manifest=json.loads(texto),
        idle_minutes=int(fila[5]) if fila[5] else DEFAULT_IDLE_MINUTES,
        bytes=len(texto.encode("utf-8")),
    )
    _guardar(release)
    return release


__all__ = [
    "MAX_BYTES",
    "ReleaseCongelada",
    "cargar_release",
    "derivar_procedimientos",
    "vaciar_cache",
]
