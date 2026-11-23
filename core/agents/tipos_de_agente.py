"""Registro de tipos de agente (ADR-0207 §1, HU-AOS-92).

Cada pack de especialidad declara su tipo en `pack_<x>/tipo_de_agente.json` (`AgentTypeManifest`). Este módulo:
- lo lee del paquete de la clase del módulo (`manifiesto_del_modulo`, detrás de `BaseModule.tipo_de_agente`);
- arma el registro `key → manifiesto` con los packs de la plataforma ya cargados por `plugin_manager`
  (`tipos_de_agente`); un plugin de un negocio nunca define tipos;
- lo traduce a `PlantillaAgente` (`a_plantilla`), la forma que siguen leyendo el editor, el alta, los presets y QA
  desde `load_plantilla` (antes `core/presets/plantillas/*.json`, que se retiraron).
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from core.mcp.types import AgentTypeManifest

if TYPE_CHECKING:
    from core.presets import PlantillaAgente

ARCHIVO = "tipo_de_agente.json"


class TipoDeAgenteInvalido(ValueError):
    """El manifiesto del tipo de un pack no es válido (forma, guion o flujo)."""


def _ruta(module: Any) -> Path | None:
    try:
        return Path(inspect.getfile(type(module))).resolve().parent / ARCHIVO
    except (TypeError, OSError):
        return None


def _secciones_del_flujo(carpeta_pack: Path) -> set[str]:
    ruta = carpeta_pack / "flow.json"
    if not ruta.is_file():
        return set()
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    secciones = datos if isinstance(datos, list) else datos.get("sections", [])
    return {str(s.get("id")) for s in secciones if isinstance(s, dict)}


@lru_cache(maxsize=32)
def _leer(ruta: str) -> AgentTypeManifest:
    archivo = Path(ruta)
    try:
        manifiesto = AgentTypeManifest.model_validate(json.loads(archivo.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise TipoDeAgenteInvalido(f"{archivo}: {exc}") from exc
    fuera = sorted(set(manifiesto.flujo_por_defecto) - _secciones_del_flujo(archivo.parent.parent))
    if fuera:
        raise TipoDeAgenteInvalido(f"{archivo}: flujo_por_defecto nombra secciones que el flow.json no tiene: {fuera}")
    a_plantilla(manifiesto)  # el guion se valida al cargar, no al primer uso
    return manifiesto


def manifiesto_del_modulo(module: Any) -> AgentTypeManifest | None:
    if getattr(module, "tipo", "") != "especialidad":
        return None
    ruta = _ruta(module)
    if ruta is None or not ruta.is_file():
        return None
    return _leer(str(ruta))


def _modulos() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.list_modules() or plugin_manager.scan()


def tipos_de_agente(modules: Iterable[Any] | None = None) -> dict[str, AgentTypeManifest]:
    """`key → manifiesto` de los packs de especialidad de la plataforma, ordenado por key."""
    from core.releases.packs import es_de_plataforma

    tipos: dict[str, AgentTypeManifest] = {}
    for module in _modulos() if modules is None else modules:
        if not es_de_plataforma(module):
            continue
        manifiesto = manifiesto_del_modulo(module)
        if manifiesto is None:
            continue
        if manifiesto.key in tipos:
            raise TipoDeAgenteInvalido(f"dos packs declaran el tipo de agente {manifiesto.key!r}")
        tipos[manifiesto.key] = manifiesto
    return dict(sorted(tipos.items()))


def a_plantilla(manifiesto: AgentTypeManifest) -> PlantillaAgente:
    from core.presets import PlantillaAgente

    try:
        return PlantillaAgente.model_validate(
            {
                "key": manifiesto.key,
                "nombre": manifiesto.nombre,
                "descripcion": manifiesto.descripcion,
                "seccion": manifiesto.seccion,
                "habilidades": manifiesto.habilidades_obligatorias,
                "opcionales": manifiesto.habilidades_opcionales,
                "acciones_opcionales": manifiesto.acciones_opcionales,
                "captar_leads": manifiesto.captar_leads,
                "procedimientos": manifiesto.procedimientos_por_defecto,
                "playbooks": manifiesto.playbooks_por_defecto,
                "reglas": manifiesto.reglas,
                "datos_de_prueba": manifiesto.datos_de_prueba,
                "preguntas": manifiesto.preguntas,
                "preguntas_retiradas": manifiesto.preguntas_retiradas,
            }
        )
    except ValidationError as exc:
        raise TipoDeAgenteInvalido(f"tipo {manifiesto.key!r}: {exc}") from exc
