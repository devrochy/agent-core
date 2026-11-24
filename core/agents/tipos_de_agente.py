"""Registro de tipos de agente (ADR-0207 §1, HU-AOS-92).

Cada pack de especialidad declara su tipo en `pack_<x>/tipo_de_agente.json` (`AgentTypeManifest`). Este módulo:
- lo lee del paquete de la clase del módulo (`manifiesto_del_modulo`, detrás de `BaseModule.tipo_de_agente`);
- arma el registro `key → manifiesto` con los packs de la plataforma (`tipos_de_agente`): de los archivos de
  `packages/` o de los módulos que se le pasen; un plugin de un negocio nunca define tipos;
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
    try:
        secciones = _secciones_del_flujo(archivo.parent.parent) if manifiesto.flujo_por_defecto else set()
    except (OSError, ValueError, AttributeError) as exc:
        raise TipoDeAgenteInvalido(f"{archivo}: flow.json ilegible: {exc}") from exc
    fuera = sorted(set(manifiesto.flujo_por_defecto) - secciones)
    if fuera:
        raise TipoDeAgenteInvalido(f"{archivo}: flujo_por_defecto nombra secciones que el flow.json no tiene: {fuera}")
    a_plantilla(manifiesto)  # el guion se valida al cargar, no al primer uso
    return manifiesto


def manifiesto_del_modulo(module: Any) -> AgentTypeManifest | None:
    from core.releases.packs import es_de_plataforma

    # Solo un pack de especialidad de la plataforma define un tipo; un plugin de un negocio nunca.
    if getattr(module, "tipo", "") != "especialidad" or not es_de_plataforma(module):
        return None
    ruta = _ruta(module)
    if ruta is None or not ruta.is_file():
        return None
    return _leer(str(ruta))


_PACKAGES = Path(__file__).resolve().parent.parent.parent / "packages"


def _desde_archivos() -> list[AgentTypeManifest]:
    """Los manifiestos de `packages/pack-*/pack_*/tipo_de_agente.json`, sin depender de qué cargó el
    `plugin_manager` en este proceso (mismo criterio que `load_fragment` con los fragmentos de presets)."""
    return [_leer(str(ruta)) for ruta in sorted(_PACKAGES.glob(f"pack-*/pack_*/{ARCHIVO}"))]


def tipos_de_agente(modules: Iterable[Any] | None = None) -> dict[str, AgentTypeManifest]:
    """`key → manifiesto` de los packs de especialidad de la plataforma, ordenado por key.

    Sin `modules` lee los archivos de `packages/` (lo que usa `load_plantilla`); con `modules` mira solo esos
    módulos (lo que usa `validar_catalogo` al arrancar)."""
    from core.releases.packs import es_de_plataforma

    if modules is None:
        manifiestos = _desde_archivos()
    else:
        manifiestos = [m for m in (manifiesto_del_modulo(x) for x in modules if es_de_plataforma(x)) if m is not None]
    tipos: dict[str, AgentTypeManifest] = {}
    for manifiesto in manifiestos:
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
                # Las `core:*` las trae todo agente: `PlantillaAgente` solo lleva packs, que son lo que se habilita
                # e instala (`asegurar_habilidades` no conoce un id `core:*`).
                "habilidades": [h for h in manifiesto.habilidades_obligatorias if not h.startswith("core:")],
                "opcionales": [h for h in manifiesto.habilidades_opcionales if not h.startswith("core:")],
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
