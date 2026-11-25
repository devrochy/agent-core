"""Catálogo de tipos de agente y de habilidades para la API (HU-AOS-94, ADR-0207 §6).

Lógica pura, sin FastAPI: la usan `GET /admin/agent-types`, `GET /admin/skill-catalog`,
`GET /admin/agents/{id}/posibles` y el 409 `habilidad_incompatible` de `PUT /admin/agents/{id}/skills/{pack}`.

- El tipo de un agente es su `source_template` o, si no, su `key` (el mismo criterio que
  `instalaciones_agente`). Un agente que no resuelve a ningún tipo (p. ej. uno con key propia) no tiene tipo:
  la API lo trata como «sin tipo», nunca como error.
- El Coordinador (`kind == "core"`) no tiene tipo de pack: su habilidad es `core:recepcion`, fija.
- El catálogo de habilidades es de la plataforma (core y packs de `packages/`): no depende del negocio ni lleva
  configuración, secretos ni instalaciones de nadie.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from core.agents.tipos_de_agente import tipos_de_agente
from core.datos_cliente import AccionDatos, registro_datos
from core.mcp.types import AgentTypeManifest, SkillCatalogItem

TIPO_DEL_COORDINADOR = "coordinador"
HABILIDAD_DEL_COORDINADOR = "core:recepcion"
KindDeHabilidad = Literal["core", "especialidad", "transversal"]


def tipo_del_agente(agente: Any) -> AgentTypeManifest | None:
    """Manifiesto del tipo de un especialista; `None` si ninguna de sus claves es un tipo."""
    tipos = tipos_de_agente()
    for clave in (getattr(agente, "source_template", None), getattr(agente, "key", None)):
        if clave and clave in tipos:
            return tipos[clave]
    return None


def transversales_admitidas(agente: Any) -> set[str] | None:
    """Habilidades transversales que el tipo del agente admite; `None` si el agente no tiene tipo (sin restricción)."""
    tipo = tipo_del_agente(agente)
    return None if tipo is None else set(tipo.habilidades_transversales)


def packs_de_especialidad_ajenos(agente: Any, especialidades: Collection[str]) -> set[str]:
    """Packs de especialidad que NO son el del tipo del agente (no se instalan a mano en otro agente)."""
    tipo = tipo_del_agente(agente)
    propios = {h for h in tipo.habilidades_obligatorias if not h.startswith("core:")} if tipo else set()
    return {p for p in especialidades if p not in propios}


def operaciones_de_datos(manifiesto: AgentTypeManifest, tools: Collection[str]) -> list[AccionDatos]:
    """Operaciones de datos del cliente del tipo: las de los packs que trae y que alguna de sus tools ejecuta.

    `registro_datos` se llena cuando cada pack se carga en el proceso: si el pack no está cargado, la lista sale
    vacía (nunca falla)."""
    packs = {h for h in manifiesto.habilidades_obligatorias if not h.startswith("core:")}
    herramientas = set(tools)
    return [a for a in registro_datos.de_packs(packs) if a.aplica_a(herramientas)]


def tipo_de_habilidad(skill: SkillCatalogItem, modulos: Collection[Any]) -> KindDeHabilidad | None:
    """`core` / `especialidad` / `transversal`; `None` si el pack no declara su tipo (no se expone)."""
    if skill.id.startswith("core:"):
        return "core"
    modulo = next((m for m in modulos if str(getattr(m, "id", "")) == skill.id), None)
    tipo = str(getattr(modulo, "tipo", "") or "")
    return tipo if tipo in ("especialidad", "transversal") else None  # type: ignore[return-value]
