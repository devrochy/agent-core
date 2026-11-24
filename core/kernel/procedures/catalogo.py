"""Catálogo de acciones del binario (HU-AOS-03) y catálogo Habilidad → Tools (ADR-0207, HU-AOS-92).

Las acciones de un procedimiento se comprueban contra lo que el binario trae: las tools
del core (`CoreSkill`, `SubscriptionSkill` y `LeadSkill`, dueño `core`) y las que declara cada módulo
en `declared_skills`. No lee la base ni el spec de ningún negocio: el catálogo es el
mismo para todos, por eso nunca incluye las tools declarativas de un negocio
(`tools_for_spec`), que sí son por negocio.

Quien llama elige los módulos: la API pasa `plugin_manager.list_modules()` (los ya
cargados; `scan()` reinstancia y reemplaza los módulos vivos) y el CLI, `scan()`.

Cada acción es un `ToolCatalogItem` (`ActionInfo` es el mismo tipo): una vista de lo que ya declaran
`MCPTool` (`side_effect`, scopes, `solo_lectura`, `sensible`) y `BaseModule.grupos_de_acciones`. El
efecto lo declara a mano cada tool (ADR-0207 §2); `validar_catalogo` exige que ninguna de la plataforma
lo deje vacío.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.mcp.types import MCPTool, SkillCatalogItem, ToolCatalogItem
from core.shared.logger import get_logger

log = get_logger(__name__)

CORE_OWNER = "core"
ActionInfo = ToolCatalogItem
Catalogo = Mapping[str, ActionInfo]

# Habilidades del core (ADR-0207 §3): ids, nombres y descripciones para el panel (texto de UI, no de prompt).
_HABILIDADES_DEL_CORE = Path(__file__).resolve().parent.parent.parent / "agents" / "habilidades_del_core.json"


def _core_skills() -> list[Any]:
    from core.leads.tools import LeadSkill
    from core.subscriptions.tools import SubscriptionSkill
    from core.tools.core_skill import CoreSkill

    return [CoreSkill(), SubscriptionSkill(), LeadSkill()]


def _skills_del_core_fuera_del_linter() -> list[Any]:
    """Tools del core que el host registra pero que ningún procedimiento puede llamar: entran al catálogo de
    habilidades (lo que el panel muestra) y NO al `Catalogo` del linter, para no cambiar qué acciones admite
    un procedimiento (revisión del arquitecto de HU-AOS-92)."""
    from core.datos_cliente.aviso_tool import AvisoDatosSkill
    from core.datos_cliente.tools import DatosClienteSkill
    from core.memory.historial_tool import HistorialClienteSkill

    return [DatosClienteSkill(), AvisoDatosSkill(), HistorialClienteSkill()]


def _item(tool: MCPTool, owner: str, group: str) -> ToolCatalogItem:
    return ToolCatalogItem(
        name=tool.name,
        owner=owner,
        side_effect=tool.side_effect,
        scopes=tuple(tool.scopes or ()),
        solo_lectura=bool(tool.solo_lectura),
        group=group,
        description=tool.description,
        requires_confirmation_default=bool(tool.sensible),
    )


def _grupo_del_core(nombre: str) -> str:
    from core.leads.tools import GRUPO as GRUPO_LEADS
    from core.leads.tools import es_de_captar_leads

    if es_de_captar_leads(nombre):
        return GRUPO_LEADS
    if nombre.startswith("sub_"):
        return "suscripciones"
    return ""


# Último catálogo armado y la identidad de los módulos de los que salió: la API lo pide
# en cada validación y los módulos cargados no cambian mientras el proceso vive.
# Guarda los módulos mismos (no sus `id`): así siguen vivos y su identidad no se recicla.
_cache: tuple[tuple[Any, ...], Mapping[str, ActionInfo]] | None = None


def catalogo_de_acciones(modules: Iterable[Any]) -> Mapping[str, ActionInfo]:
    """Acciones del binario por nombre: las del core más las de `modules`.

    Se cachea por la identidad de los módulos (los mismos objetos → el mismo catálogo).
    Una skill que no se puede instanciar se salta con un log (mismo criterio que
    `agent-core flow validate`): sus acciones aparecerán como inexistentes."""
    global _cache
    clave = tuple(modules)
    if _cache is not None and len(_cache[0]) == len(clave) and all(
        a is b for a, b in zip(_cache[0], clave, strict=True)
    ):
        return _cache[1]
    catalogo: Mapping[str, ActionInfo] = MappingProxyType(_armar(list(clave)))
    _cache = (clave, catalogo)
    return catalogo


def _tools_del_modulo(module: Any) -> list[MCPTool]:
    tools: list[MCPTool] = []
    for skill_cls in getattr(module, "declared_skills", None) or []:
        try:
            tools.extend(skill_cls().get_tools())
        except Exception:
            log.warning("procedimientos_catalogo_skill_fallo", plugin=str(getattr(module, "id", "")), exc_info=True)
    return tools


def _armar(modules: list[Any]) -> dict[str, ActionInfo]:
    from core.releases.packs import es_de_plataforma

    catalogo: dict[str, ActionInfo] = {}
    # Los packs de la plataforma primero: si el plugin de un negocio repite el nombre de una tool, el efecto que rige
    # (cambio de release, linter) es el del pack, nunca el que declare ese plugin (auditoría de HU-AOS-92).
    modules = sorted(modules, key=lambda m: not es_de_plataforma(m))
    for skill in _core_skills():
        for tool in skill.get_tools():
            catalogo[tool.name] = _item(tool, CORE_OWNER, _grupo_del_core(tool.name))
    for module in modules:
        owner = str(getattr(module, "id", "") or "")
        grupos = getattr(module, "grupos_de_acciones", None) or {}
        for tool in _tools_del_modulo(module):
            # Una tool repetida es un error de `validar_catalogo`; aquí gana la primera para que el runtime no
            # cambie de dueño según el orden de carga.
            catalogo.setdefault(tool.name, _item(tool, owner, grupos.get(tool.name, "")))
    return catalogo


# ------------------------------------------------------------------ habilidades
@lru_cache(maxsize=1)
def _textos_del_core() -> dict[str, dict[str, Any]]:
    datos = json.loads(_HABILIDADES_DEL_CORE.read_text(encoding="utf-8"))
    return {h["id"]: h for h in datos["habilidades"]}


def catalogo_de_habilidades(modules: Iterable[Any]) -> list[SkillCatalogItem]:
    """Cada habilidad con sus tools (ADR-0207 §3): las del core (`core:*`) y los packs de la plataforma.

    - Las `sub_*` del core las despacha `pack_subscriptions` (`TOOLS_DEL_CORE_POR_PACK`): en el catálogo son de esa
      habilidad aunque su dueño en runtime siga siendo `core` (el linter las admite sin `requires`).
    - `core:recepcion` es la vista del Coordinador (ADR-0205 §5): repite tools de otras habilidades a propósito.
    """
    from core.agents.recepcion import TOOLS_RECEPCION
    from core.mcp.host import TOOLS_DEL_CORE_POR_PACK
    from core.releases.packs import es_de_plataforma

    modulos = [m for m in modules if es_de_plataforma(m)]
    catalogo = catalogo_de_acciones(modulos)
    textos = _textos_del_core()
    del_core: dict[str, list[ToolCatalogItem]] = {k: [] for k in textos}
    de_pack: dict[str, list[ToolCatalogItem]] = {str(m.id): [] for m in modulos}
    extra = [
        _item(t, CORE_OWNER, "")
        for s in _skills_del_core_fuera_del_linter()
        for t in s.get_tools()
    ]
    for item in [*catalogo.values(), *extra]:
        if item.owner != CORE_OWNER:
            de_pack.setdefault(item.owner, []).append(item)
            continue
        pack = next((p for pre, p in TOOLS_DEL_CORE_POR_PACK.items() if item.name.startswith(pre)), None)
        if pack is not None:
            de_pack.setdefault(pack, []).append(item)
            continue
        habilidad = next((k for k, h in textos.items() if item.name in h.get("tools", ())), None)
        if habilidad is not None:
            del_core[habilidad].append(item)
    for nombre in TOOLS_RECEPCION:
        item = catalogo.get(nombre) or next((e for e in extra if e.name == nombre), None)
        if item is not None and "core:recepcion" in del_core:
            del_core["core:recepcion"].append(item)
    salida = [
        SkillCatalogItem(id=k, nombre=textos[k]["nombre"], descripcion=textos[k]["descripcion"], tools=tuple(v))
        for k, v in del_core.items()
    ]
    for module in modulos:
        salida.append(
            SkillCatalogItem(
                id=str(module.id),
                nombre=str(getattr(module, "display_name", "") or getattr(module, "name", "") or module.id),
                descripcion=str(getattr(module, "description", "") or ""),
                tools=tuple(sorted(de_pack.get(str(module.id), []), key=lambda t: t.name)),
            )
        )
    return salida


# ------------------------------------------------------------------ validación
def validar_catalogo(modules: Iterable[Any]) -> list[str]:
    """Problemas del registro de habilidades y tipos de agente (ADR-0207 §4). Vacío = válido.

    Solo mira el core y los packs de la plataforma: un plugin de un negocio nunca bloquea el arranque."""
    from core.agents.tipos_de_agente import tipos_de_agente
    from core.kernel.procedures.de_pack import cargar
    from core.releases.packs import es_de_plataforma

    modulos = [m for m in modules if es_de_plataforma(m)]
    problemas: list[str] = []
    duenos: dict[str, str] = {}

    def _ver(tool: MCPTool, dueno: str, grupo: str | None) -> None:
        if tool.name in duenos and duenos[tool.name] != dueno:
            problemas.append(f"la tool {tool.name!r} la declaran {duenos[tool.name]!r} y {dueno!r}")
        elif tool.name in duenos:
            problemas.append(f"la tool {tool.name!r} está repetida en {dueno!r}")
        duenos[tool.name] = dueno
        if tool.side_effect is None:
            problemas.append(f"la tool {tool.name!r} de {dueno!r} no declara side_effect")
        if grupo is not None and not grupo:
            problemas.append(f"la tool {tool.name!r} de {dueno!r} no tiene grupo en grupos_de_acciones")

    for skill in [*_core_skills(), *_skills_del_core_fuera_del_linter()]:
        for tool in skill.get_tools():
            _ver(tool, CORE_OWNER, None)
    for module in modulos:
        grupos = getattr(module, "grupos_de_acciones", None) or {}
        for tool in _tools_del_modulo(module):
            _ver(tool, str(module.id), grupos.get(tool.name, ""))

    textos = _textos_del_core()
    # Toda tool del core (salvo las que despacha un pack) está en una habilidad `core:*`: si no, el catálogo la
    # dejaría fuera sin aviso.
    from core.mcp.host import TOOLS_DEL_CORE_POR_PACK

    del_core = {t.name for s in [*_core_skills(), *_skills_del_core_fuera_del_linter()] for t in s.get_tools()}
    con_habilidad: dict[str, str] = {}
    for hid, h in textos.items():
        for nombre in h.get("tools", ()):
            if nombre not in del_core:
                problemas.append(f"la habilidad {hid!r} nombra la tool {nombre!r}, que el core no trae")
            if nombre in con_habilidad:
                problemas.append(f"la tool {nombre!r} está en {con_habilidad[nombre]!r} y en {hid!r}")
            con_habilidad[nombre] = hid
    for nombre in sorted(del_core - set(con_habilidad)):
        if not nombre.startswith(tuple(TOOLS_DEL_CORE_POR_PACK)):
            problemas.append(f"la tool del core {nombre!r} no está en ninguna habilidad del core")
    habilidades = {*textos, *(str(m.id) for m in modulos)}
    catalogo = catalogo_de_acciones(modulos)
    for tipo in tipos_de_agente(modulos).values():
        propias = {*tipo.habilidades_obligatorias, *tipo.habilidades_opcionales}
        for h in sorted(propias - habilidades):
            problemas.append(f"el tipo {tipo.key!r} declara la habilidad {h!r}, que no existe")
        # Las acciones de un procedimiento por defecto tienen que estar en las habilidades del tipo o en el core.
        for proc_id in tipo.procedimientos_por_defecto:
            proc = next((p for p in cargar(proc_id.partition(".")[0]) if p.id == proc_id), None)
            if proc is None:
                problemas.append(f"el tipo {tipo.key!r} declara el procedimiento {proc_id!r}, que su pack no publica")
                continue
            for stage in proc.stages:
                for accion in [*stage.actions, *stage.preconditions]:
                    info = catalogo.get(accion)
                    if info is None or (info.owner != CORE_OWNER and info.owner not in propias):
                        problemas.append(
                            f"el procedimiento {proc_id!r} del tipo {tipo.key!r} usa {accion!r}, "
                            "que no está en las habilidades del tipo"
                        )
    return problemas
