"""Catálogo de acciones del binario para el linter de procedimientos (HU-AOS-03).

Las acciones de un procedimiento se comprueban contra lo que el binario trae: las tools
del core (`CoreSkill` y `SubscriptionSkill`, dueño `core`) y las que declara cada módulo
en `declared_skills`. No lee la base ni el spec de ningún negocio: el catálogo es el
mismo para todos, por eso nunca incluye las tools declarativas de un negocio
(`tools_for_spec`), que sí son por negocio.

Quien llama elige los módulos: la API pasa `plugin_manager.list_modules()` (los ya
cargados; `scan()` reinstancia y reemplaza los módulos vivos) y el CLI, `scan()`.

`side_effect` queda en `None` para todas las acciones reales: el efecto de cada acción
lo declara el manifiesto de acciones v2 (HU-AOS-34, ADR-0118 §4). Hasta entonces la
regla de irreversibles del linter solo actúa con un catálogo que lo traiga.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentcore_contracts import SideEffect

from core.shared.logger import get_logger

log = get_logger(__name__)

CORE_OWNER = "core"


@dataclass(frozen=True)
class ActionInfo:
    name: str
    owner: str
    side_effect: SideEffect | None = None


Catalogo = Mapping[str, ActionInfo]


def _core_skills() -> list[Any]:
    from core.subscriptions.tools import SubscriptionSkill
    from core.tools.core_skill import CoreSkill

    return [CoreSkill(), SubscriptionSkill()]


def catalogo_de_acciones(modules: Iterable[Any]) -> dict[str, ActionInfo]:
    """Acciones del binario por nombre: las del core más las de `modules`.

    Una skill que no se puede instanciar se salta con un log (mismo criterio que
    `agent-core flow validate`): sus acciones aparecerán como inexistentes."""
    catalogo: dict[str, ActionInfo] = {}
    for skill in _core_skills():
        for tool in skill.get_tools():
            catalogo[tool.name] = ActionInfo(name=tool.name, owner=CORE_OWNER)
    for module in modules:
        owner = str(getattr(module, "id", "") or "")
        for skill_cls in getattr(module, "declared_skills", None) or []:
            try:
                tools = skill_cls().get_tools()
            except Exception:
                log.warning("procedimientos_catalogo_skill_fallo", plugin=owner, exc_info=True)
                continue
            for tool in tools:
                catalogo.setdefault(tool.name, ActionInfo(name=tool.name, owner=owner))
    return catalogo
