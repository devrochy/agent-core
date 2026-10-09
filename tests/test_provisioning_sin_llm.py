"""HU-SH-03 · Gherkin «Sin el LLM» (ADR-0120 § 4).

Dado cualquier conversación, cuando el asistente intenta crear un negocio, entonces no
tiene ninguna tool ni token para hacerlo. Se recorren TODAS las tools que el core y los
packs instalados pueden exponer al modelo y se verifica, sin depender de nombres:

- que ningún handler vive en el alta (`core.onboarding`, `core.provisioning`);
- que ningún módulo que define tools importa el consumidor del alta ni usa
  `onboard_tenant`, ni siquiera dentro de una función;
- que ninguna tool tiene nombre de "crear negocio".

El contrato `alta-fuera-del-llm` de `.importlinter` cubre los imports directos en el CI
("Capas").
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

import pytest

# Lo que crea negocios. `core.provisioning.fuentes` es el contrato que implementan los packs
# (datos del pedido): no crea nada. De `core.onboarding` se prohíbe el alta, no sus
# utilidades (`tenant_dir`, que usa cobranza para las evidencias).
_PROHIBIDOS = ("core.provisioning.consumer", "core.provisioning.owner")
_SIMBOLOS_PROHIBIDOS = ("onboard_tenant", "aprovisionar", "on_subscription_activated")
_NOMBRE_DE_ALTA = re.compile(
    r"(crear|create|alta|nuevo|new|registrar|register)_?(tenant|negocio|business)|provision|onboard",
    re.IGNORECASE,
)


def _tools_del_llm() -> list:  # type: ignore[type-arg]
    from core.mcp.host import MCPHost
    from core.plugin_manager import plugin_manager
    from core.tools.core_skill import register_core_skills

    host = MCPHost()
    register_core_skills(host)
    plugin_manager.scan()
    for module in plugin_manager.list_modules():
        host.begin_activation(module.id)
        try:
            for skill_cls in getattr(module, "declared_skills", []) or []:
                host.register_skill(skill_cls())
        finally:
            host.end_activation()
    return host.get_tools()


def _usa(path: Path, simbolo: str) -> bool:
    """¿El archivo nombra `simbolo` (import, llamada o atributo), en cualquier nivel?"""
    arbol = ast.parse(path.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.alias) and simbolo in (nodo.name, nodo.asname):
            return True
        if isinstance(nodo, ast.Name) and nodo.id == simbolo:
            return True
        if isinstance(nodo, ast.Attribute) and nodo.attr == simbolo:
            return True
    return False


def _importa(path: Path, modulo: str) -> bool:
    """¿El archivo importa `modulo` (o algo dentro de él), en cualquier nivel?"""
    arbol = ast.parse(path.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres = [a.name for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres = [nodo.module] + [f"{nodo.module}.{a.name}" for a in nodo.names]
        else:
            continue
        if any(n == modulo or n.startswith(f"{modulo}.") for n in nombres):
            return True
    return False


@pytest.fixture(scope="module")
def tools() -> list:  # type: ignore[type-arg]
    encontradas = _tools_del_llm()
    nombres = {t.name for t in encontradas}
    # Cordura: el recorrido ve las tools de verdad (core, suscripciones y packs).
    assert {"escalate_to_human", "sub_cambiar_plan", "crear_pedido"} <= nombres
    return encontradas


def test_ningun_handler_de_tool_vive_en_el_alta(tools: list) -> None:  # type: ignore[type-arg]
    for tool in tools:
        modulo = inspect.getmodule(tool.handler)
        assert modulo is not None, tool.name
        assert not modulo.__name__.startswith(("core.onboarding", "core.provisioning")), tool.name


def test_ningun_modulo_de_tools_importa_el_alta(tools: list) -> None:  # type: ignore[type-arg]
    modulos = {inspect.getmodule(t.handler).__name__ for t in tools}  # type: ignore[union-attr]
    for nombre in sorted(modulos):
        archivo = Path(inspect.getfile(sys.modules[nombre]))
        for prohibido in _PROHIBIDOS:
            assert not _importa(archivo, prohibido), f"{nombre} importa {prohibido}"
        for simbolo in _SIMBOLOS_PROHIBIDOS:
            assert not _usa(archivo, simbolo), f"{nombre} usa {simbolo}"


def test_ninguna_tool_se_llama_como_un_alta(tools: list) -> None:  # type: ignore[type-arg]
    assert [t.name for t in tools if _NOMBRE_DE_ALTA.search(t.name)] == []


def test_el_consumidor_solo_se_dispara_por_el_outbox() -> None:
    """El alta solo se registra como handler de `subscription.activated` en el despachador:
    ninguna ruta de la API ni tool la importa."""
    from core.events import core_handlers
    from core.provisioning.consumer import on_subscription_activated
    from core.shared.events import ON_SUBSCRIPTION_ACTIVATED

    assert (ON_SUBSCRIPTION_ACTIVATED, on_subscription_activated) in core_handlers._HANDLERS
    raiz = Path(__file__).resolve().parents[1] / "core" / "api"
    for archivo in raiz.rglob("*.py"):
        for prohibido in _PROHIBIDOS:
            assert not _importa(archivo, prohibido), f"{archivo} importa {prohibido}"
