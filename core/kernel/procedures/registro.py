"""Procedimientos disponibles en runtime (HU-AOS-05, ADR-0113 §1).

Los procedimientos los publica cada habilidad al activarse (`MCPHost.register_procedures`) y
sus *resolvers* con `MCPHost.register_resolver`. Este módulo es el único lector del motor:

- `disponibles(enabled, disabled)`: los de los packs habilitados y sanos del negocio cuyo
  `requires` esté entero habilitado (fail-closed: si falta una habilidad, no se ofrece).
- Un procedimiento que no pasa el linter contra el catálogo del binario no se usa (log, una vez
  por proceso). El CI ya corre `revisar`; esto es defensa en profundidad.
- `definicion(id, version)`: la definición EXACTA que gobierna una tarea. Si la versión de una
  tarea abierta ya no está en el binario (un deploy subió la versión), devuelve None: el motor no
  gobierna esa tarea (queda el flujo v2 de respaldo) hasta la revalidación de HU-AOS-14.

Transitorio hasta HU-AOS-13/14: la fuente son los procedimientos del binario de los packs
habilitados, no los `procedures` de la versión del agente ni la release de la conexión.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

from agentcore_contracts import Procedure

from core.kernel.procedures.catalogo import CORE_OWNER, catalogo_de_acciones
from core.kernel.procedures.linter import lint
from core.mcp.host import mcp_host
from core.mcp.types import Resolver
from core.shared.logger import get_logger

log = get_logger(__name__)

# (id, versión) → resultado del linter. Los procedimientos del binario no cambian mientras el
# proceso vive (si un plugin se recarga, sus objetos son otros y se vuelven a revisar).
_revisados: dict[tuple[str, str, int], bool] = {}


def _todos() -> list[Procedure]:
    return [p for lista in mcp_host.get_procedures().values() for p in lista]


def hay_procedimientos() -> bool:
    """¿Algún pack registró procedimientos? Chequeo en memoria, sin BD (costo del modo sombra
    y del cierre por evento cuando no hay ninguno)."""
    return any(mcp_host.get_procedures().values())


def _valido(proc: Procedure) -> bool:
    clave = (proc.id, proc.version, id(proc))
    if clave not in _revisados:
        from core.plugin_manager import plugin_manager

        problemas = lint(proc, catalogo_de_acciones(plugin_manager.list_modules()))
        if problemas:
            log.error(
                "procedimiento_invalido_en_runtime",
                procedure_id=proc.id,
                version=proc.version,
                codigos=sorted({p.code for p in problemas})[:10],
            )
        _revisados[clave] = not problemas
    return _revisados[clave]


def disponibles(enabled: Iterable[str], disabled: Iterable[str] = ()) -> dict[str, Procedure]:
    """Procedimientos que el negocio puede usar en este turno, por id."""
    habilitados = set(enabled) - set(disabled)
    habilitados.add(CORE_OWNER)
    salida: dict[str, Procedure] = {}
    for plugin_id, lista in mcp_host.get_procedures().items():
        if plugin_id not in habilitados:
            continue
        for proc in lista:
            if all(r in habilitados for r in proc.requires) and _valido(proc):
                salida[proc.id] = proc
    return salida


def definicion(procedure_id: str, version: str) -> Procedure | None:
    """Definición exacta de una tarea (id y versión), o None si el binario no la trae."""
    for proc in _todos():
        if proc.id == procedure_id and proc.version == version:
            return proc if _valido(proc) else None
    return None


def gobernable(proc: Procedure, permitidos: Iterable[str]) -> bool:
    """La habilidad del procedimiento (y todo su `requires`) está habilitada y sana en el negocio."""
    habilitados = {*permitidos, CORE_OWNER}
    return proc.owner in habilitados and all(r in habilitados for r in proc.requires)


def resolver(nombre: str, permitidos: Iterable[str]) -> Resolver | None:
    """El resolver, solo si su habilidad está habilitada y sana en el negocio."""
    r = mcp_host.get_resolver(nombre)
    return r if r is not None and r.plugin_id in {*permitidos, CORE_OWNER} else None


async def packs_permitidos(tenant_id: uuid.UUID) -> set[str]:
    """Packs habilitados y sanos del negocio (fuera del loop: consumer y worker)."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.plugin_manager import plugin_manager

    spec = await load_active_spec(tenant_id)
    return set(enabled_packs(spec)) - set(await plugin_manager.disabled_plugins_for_tenant(tenant_id))


def con_evento(topic: str) -> Mapping[str, Procedure]:
    """Procedimientos cuyo cierre declara el topic del outbox (`<dueño>.<topic>`)."""
    return {
        p.id: p
        for p in _todos()
        if p.completion is not None and any(e.split(".", 1)[1] == topic for e in p.completion.events)
    }
