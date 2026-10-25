"""Qué se puede encargar y consultar a otro agente (ADR-0116, adenda HU-AOS-19 §4-§5). Puro.

- **Encargable:** el destino lo expone (`exposes.procedures`), lo publica en la release (versión
  congelada del destino) y el procedimiento **se completa con datos**:
  - ninguna etapa tiene acción de salida, confirmación, datos `confirmed` en la salida ni
    precondiciones, y no espera eventos de cierre;
  - todo dato que pide una salida es escribible;
  - ningún dato tiene `confirm` ni es de clase `payment`.
  Lo demás no se ofrece (fail-closed).
- **Consultable:**
  - el destino declara la consulta con `action` y esa acción está entre sus tools habilitadas y en
    el scope de la release;
  - la acción no aparece en `stage.actions` de ningún procedimiento de la release ni pide
    confirmación en el binding del destino (si no, la consulta saltaría el ligado y la confirmación
    del motor).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentcore_contracts import Procedure

from core.kernel.procedures import comandos, motor
from core.kernel.procedures.catalogo import CORE_OWNER


def encargable(proc: Procedure) -> bool:
    if proc.completion is not None and proc.completion.events:
        return False
    escribibles = comandos.escribibles(proc)
    if any(s.confirm or s.dataClass == "payment" for s in proc.slots.values()):
        return False
    for stage in proc.stages:
        if stage.confirmation is not None or stage.preconditions:
            return False
        if motor.es_terminal(stage):
            continue
        salida = stage.exitWhen
        assert salida is not None
        if salida.action is not None or salida.confirmed:
            return False
        if any(nombre not in escribibles for nombre in salida.slots):
            return False
    return True


def _expuestos(destino: Any) -> list[str]:
    exposes = destino.content.get("exposes")
    valores = exposes.get("procedures") if isinstance(exposes, Mapping) else None
    return [str(v) for v in valores] if isinstance(valores, list) else []


def encargables_de(destino: Any, publicados: Mapping[str, Procedure]) -> dict[str, Procedure]:
    """Los procedimientos que `destino` acepta encargados (id → definición congelada)."""
    return {
        pid: publicados[pid]
        for pid in sorted(set(_expuestos(destino)))
        if pid in publicados and encargable(publicados[pid])
    }


@dataclass(frozen=True)
class Consulta:
    id: str
    descripcion: str
    accion: str
    output_schema: Mapping[str, Any]


def gobernadas_por_el_motor(procedimientos: Iterable[Procedure]) -> frozenset[str]:
    """Acciones que aparecen en alguna etapa: las gobierna el motor (ligado y confirmación)."""
    return frozenset(a for p in procedimientos for s in p.stages for a in s.actions)


def consultas_de(
    destino: Any, *, prohibidas: frozenset[str], packs: frozenset[str], plugin_de: Callable[[str], str | None]
) -> dict[str, Consulta]:
    """Las consultas que `destino` responde (id → consulta). `plugin_de`: dueño de cada acción."""
    exposes = destino.content.get("exposes")
    crudas = exposes.get("consultas") if isinstance(exposes, Mapping) else None
    propias: dict[str, bool] = {}
    for t in destino.content.get("tools") or []:
        if isinstance(t, Mapping) and isinstance(t.get("tool"), str) and t.get("enabled", True):
            propias[t["tool"]] = bool(t.get("requiresConfirmation", False))
    salida: dict[str, Consulta] = {}
    for c in crudas if isinstance(crudas, list) else []:
        if not isinstance(c, Mapping) or not isinstance(c.get("id"), str):
            continue
        accion = c.get("action")
        if not isinstance(accion, str) or accion not in propias or propias[accion] or accion in prohibidas:
            continue
        plugin = plugin_de(accion)
        if plugin is None or (plugin != CORE_OWNER and plugin not in packs):
            continue
        esquema = c.get("outputSchema")
        salida[c["id"]] = Consulta(
            c["id"], str(c.get("description") or ""), accion, esquema if isinstance(esquema, Mapping) else {"type": "object"}
        )
    return salida


def clases_de(proc: Procedure, nombres: Iterable[str]) -> tuple[str, ...]:
    """Clases de datos de lo que se envía, en las del grafo (`public`, `pii`)."""
    clases = sorted({proc.slots[n].dataClass for n in nombres if n in proc.slots})
    return tuple(clases) or ("public",)


__all__ = ["Consulta", "clases_de", "consultas_de", "encargable", "encargables_de", "gobernadas_por_el_motor"]
