"""Diferencias legibles entre dos releases de una conexión (HU-AOS-27, adenda de ADR-0117). Puro.

Para «Ver diferencias» en el panel: qué cambia si se publica la candidata frente a otra release (por
defecto, la activa), agrupado como lo entiende el dueño:

- **agentes**: alta, baja, otra versión y cambio de agente por defecto, con las **claves de primer
  nivel** del contenido que cambiaron (el panel las traduce; nunca textos: un prompt, una URL o un
  header pueden traer datos o secretos); herramientas y procedimientos añadidos y quitados por nombre
  (de `declarativeTools`, solo el `name`);
- **superpoderes**: habilidades añadidas, quitadas o con otra versión, y plugins del negocio;
- **permisos**: enlaces A2A añadidos, quitados o con otros modos;
- **otros**: campos de `app_settings` que cambiaron (solo el nombre), overrides de playbooks y el flujo;
- **medira**: los procedimientos que el gate medirá (`procedimientos_tocados`).

Los nombres legibles (agentes y habilidades) los pasa quien llama: este módulo no lee la base.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.releases.cargar import ReleaseCongelada
from core.releases.diferencias import procedimientos_tocados

# Claves del contenido que no se reportan (la forma del contrato, no un cambio del dueño).
_IGNORADAS = frozenset({"contract"})


def _canon(valor: Any) -> str:
    return json.dumps(valor, sort_keys=True, ensure_ascii=False, default=str)


def _nombres_de(valores: Any, clave: str) -> set[str]:
    return {
        str(v.get(clave))
        for v in valores or []
        if isinstance(v, Mapping) and isinstance(v.get(clave), str) and v.get("enabled", True)
    }


def _cambios_de_contenido(antes: Mapping[str, Any], ahora: Mapping[str, Any], role: str) -> dict[str, Any]:
    """Las claves de primer nivel que cambiaron (estables: el panel las traduce y manda las que no
    conoce a «otros cambios») y, en un especialista, lo añadido y quitado por nombre."""
    cambiadas = sorted(
        k for k in set(antes) | set(ahora) if k not in _IGNORADAS and _canon(antes.get(k)) != _canon(ahora.get(k))
    )
    salida: dict[str, Any] = {"secciones": cambiadas}
    if role != "core":
        t_antes, t_ahora = _nombres_de(antes.get("tools"), "tool"), _nombres_de(ahora.get("tools"), "tool")
        p_antes, p_ahora = _nombres_de(antes.get("procedures"), "id"), _nombres_de(ahora.get("procedures"), "id")
        d_antes = _nombres_de(antes.get("declarativeTools"), "name")
        d_ahora = _nombres_de(ahora.get("declarativeTools"), "name")
        salida.update(
            {
                "herramientas_agregadas": sorted((t_ahora - t_antes) | (d_ahora - d_antes)),
                "herramientas_quitadas": sorted((t_antes - t_ahora) | (d_antes - d_ahora)),
                "procedimientos_agregados": sorted(p_ahora - p_antes),
                "procedimientos_quitados": sorted(p_antes - p_ahora),
            }
        )
    return salida


def _agentes(candidata: ReleaseCongelada, base: ReleaseCongelada | None, nombres: Mapping[str, str]) -> list[dict[str, Any]]:
    previos = {c.instance_id: c for c in (base.componentes if base else ())}
    # Una base `release/1` no congeló `is_default`: no se inventa un cambio de agente por defecto.
    compara_defecto = base is not None and base.con_procedimientos_por_agente
    salida: list[dict[str, Any]] = []
    for c in candidata.componentes:
        previo = previos.pop(c.instance_id, None)
        nombre = nombres.get(str(c.agent_id)) or c.key
        if previo is None:
            salida.append({"key": c.key, "nombre": nombre, "rol": c.role, "cambio": "nuevo", "version_antes": None, "version_ahora": c.version})
            continue
        cambio_defecto = compara_defecto and previo.is_default != c.is_default
        if previo.version_id == c.version_id and not cambio_defecto:
            continue
        item: dict[str, Any] = {
            "key": c.key, "nombre": nombre, "rol": c.role, "cambio": "actualizado",
            "version_antes": previo.version, "version_ahora": c.version,
        }
        if previo.version_id != c.version_id:
            item.update(_cambios_de_contenido(previo.content, c.content, c.role))
        if cambio_defecto:
            item["por_defecto"] = c.is_default
            if previo.version_id == c.version_id:
                item["cambio"] = "por_defecto"
        salida.append(item)
    for previo in previos.values():
        salida.append(
            {
                "key": previo.key, "nombre": nombres.get(str(previo.agent_id)) or previo.key, "rol": previo.role,
                "cambio": "retirado", "version_antes": previo.version, "version_ahora": None,
            }
        )
    orden = {"core": 0, "specialist": 1, "backoffice": 2}
    return sorted(salida, key=lambda a: (orden.get(a["rol"], 9), a["nombre"]))


def _superpoderes(candidata: ReleaseCongelada, base: ReleaseCongelada | None, nombres: Mapping[str, str]) -> dict[str, Any]:
    def versiones(r: ReleaseCongelada | None) -> dict[str, str]:
        return {str(s.get("pack_id")): str(s.get("version")) for s in (r.manifest.get("skills") if r else []) or [] if isinstance(s, Mapping)}

    antes, ahora = versiones(base), versiones(candidata)
    cambios: list[dict[str, Any]] = []
    for pack in sorted(set(antes) | set(ahora)):
        if antes.get(pack) == ahora.get(pack):
            continue
        cambio = "agregada" if pack not in antes else "quitada" if pack not in ahora else "nueva_version"
        cambios.append({"pack_id": pack, "nombre": nombres.get(pack) or pack, "cambio": cambio, "antes": antes.get(pack), "ahora": ahora.get(pack)})
    p_antes = set(base.tenant_plugins) if base else set()
    p_ahora = set(candidata.tenant_plugins)
    return {
        "habilidades": cambios,
        "plugins_agregados": sorted(p_ahora - p_antes),
        "plugins_quitados": sorted(p_antes - p_ahora),
    }


def _permisos(candidata: ReleaseCongelada, base: ReleaseCongelada | None, nombres: Mapping[str, str]) -> list[dict[str, Any]]:
    def enlaces(r: ReleaseCongelada | None) -> dict[tuple[str, str], list[str]]:
        grafo = r.a2a if r else None
        return {
            (str(e.get("from_agent_id")), str(e.get("to_agent_id"))): sorted(e.get("modes") or [])
            for e in (grafo or {}).get("links") or []
            if isinstance(e, Mapping)
        }

    antes, ahora = enlaces(base), enlaces(candidata)
    salida: list[dict[str, Any]] = []
    for par in sorted(set(antes) | set(ahora)):
        if antes.get(par) == ahora.get(par):
            continue
        cambio = "agregado" if par not in antes else "quitado" if par not in ahora else "modos"
        salida.append(
            {
                "desde": nombres.get(par[0]) or "un agente",
                "hacia": nombres.get(par[1]) or "un agente",
                "cambio": cambio,
                "modos_antes": antes.get(par, []),
                "modos_ahora": ahora.get(par, []),
            }
        )
    return salida


def _otros(candidata: ReleaseCongelada, base: ReleaseCongelada | None) -> dict[str, Any]:
    a_antes = base.app_settings if base else {}
    a_ahora = candidata.app_settings
    pb_antes = base.playbooks if base else {}
    pb_ahora = candidata.playbooks
    return {
        "ajustes": sorted(k for k in set(a_antes) | set(a_ahora) if a_antes.get(k) != a_ahora.get(k)),
        "playbooks": sorted(n for n in set(pb_antes) | set(pb_ahora) if pb_antes.get(n) != pb_ahora.get(n)),
        "flujo": _canon(base.flow_tenant if base else None) != _canon(candidata.flow_tenant),
    }


def diferencias(
    candidata: ReleaseCongelada,
    base: ReleaseCongelada | None,
    *,
    nombres_de_agentes: Mapping[str, str],
    nombres_de_habilidades: Mapping[str, str],
) -> dict[str, Any]:
    """El diff agrupado. `base` None = la candidata es la primera (todo es nuevo)."""
    medira = procedimientos_tocados(candidata, base)
    titulos = {pid: p.title for pid, p in candidata.procedimientos().items()}
    return {
        "candidata": candidata.number,
        "base": base.number if base else None,
        "sin_linea_base": base is None or not base.con_procedimientos_por_agente,
        "agentes": _agentes(candidata, base, nombres_de_agentes),
        "superpoderes": _superpoderes(candidata, base, nombres_de_habilidades),
        "permisos": _permisos(candidata, base, nombres_de_agentes),
        "otros": _otros(candidata, base),
        "medira": [
            {"procedure_id": pid, "titulo": titulos.get(pid), "motivo": motivo} for pid, motivo in sorted(medira.items())
        ],
    }


__all__ = ["diferencias"]
