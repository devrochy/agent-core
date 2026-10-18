"""Resolución de los procedimientos de un agente contra las habilidades de una release (HU-AOS-16,
ADR-0118 adenda HU-AOS-16, ADR-0113 §2 y §4). Puro: sin base ni binario.

Cada `ProcedureRef` de una versión de agente (`procedures[].{id, version}`) resuelve a UNA versión
exacta de un procedimiento publicado por una habilidad de la release, o a un procedimiento abierto
(`=0.0.0`, los que arma el lector tolerante desde el flujo v2). Rangos (`VERSION_RANGE_PATTERN` del
contrato):

- `^M[.m[.p]]`: compatible con el major (semántica de npm: con major 0 se fija el minor);
- `~M[.m[.p]]`: compatible con el minor;
- `=X[.Y[.Z]]` o `X[.Y[.Z]]`: exacta (las partes que faltan son 0).

Un procedimiento publicado cuyo `requires` no está entero entre las habilidades de la release no
resuelve (fail-closed, ADR-0113 §1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentcore_contracts import Procedure

OPEN_VERSION = "0.0.0"
CORE = "core"

Version = tuple[int, int, int]


def parsear_version(texto: str) -> Version:
    partes = [int(p) for p in texto.split(".")]
    while len(partes) < 3:
        partes.append(0)
    return partes[0], partes[1], partes[2]


def major(texto: str) -> int:
    return parsear_version(texto)[0]


def cumple(version: str, rango: str) -> bool:
    """¿`version` (semver exacta) satisface `rango`? Un texto mal formado no cumple."""
    try:
        return _cumple(version, rango)
    except ValueError:
        return False


def _cumple(version: str, rango: str) -> bool:
    v = parsear_version(version)
    prefijo = rango[0] if rango and rango[0] in "^~=" else ""
    base_texto = rango[1:] if prefijo else rango
    base = parsear_version(base_texto)
    partes = base_texto.count(".") + 1
    if prefijo == "^":
        if base[0] > 0:
            tope: Version = (base[0] + 1, 0, 0)
        elif base[1] > 0 or partes < 2:
            tope = (0, base[1] + 1, 0)
        else:
            tope = (0, 0, base[2] + 1) if partes == 3 else (0, 1, 0)
        return base <= v < tope
    if prefijo == "~":
        tope = (base[0], base[1] + 1, 0) if partes >= 2 else (base[0] + 1, 0, 0)
        return base <= v < tope
    return v == base


def es_abierto(rango: str) -> bool:
    return rango in (OPEN_VERSION, f"={OPEN_VERSION}")


def rango_compatible(version: str) -> str:
    """El rango con el que el lector tolerante declara un procedimiento publicado: `^M.0`."""
    return f"^{major(version)}.0"


@dataclass(frozen=True)
class Resuelto:
    id: str
    version: str


def ofrecible(proc: Procedure, habilidades: Iterable[str]) -> bool:
    """La habilidad del procedimiento y todo su `requires` están en la release."""
    presentes = {*habilidades, CORE}
    return proc.owner in presentes and all(r in presentes for r in proc.requires)


def publicados_por_id(procedimientos: Iterable[Procedure]) -> dict[str, list[Procedure]]:
    salida: dict[str, list[Procedure]] = {}
    for proc in procedimientos:
        salida.setdefault(proc.id, []).append(proc)
    return salida


def resolver_refs(
    refs: Iterable[Mapping[str, Any]],
    *,
    abiertos: Iterable[str],
    publicados: Mapping[str, list[Procedure]],
    habilidades: Iterable[str],
) -> tuple[list[Resuelto], list[str]]:
    """(resueltos, problemas). Un ref abierto resuelve si el flujo deriva ese id; un rango, a la
    versión más alta publicada por una habilidad de la release que lo cumpla y sea ofrecible."""
    ids_abiertos = set(abiertos)
    skills = set(habilidades)
    resueltos: list[Resuelto] = []
    problemas: list[str] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        pid, rango = ref.get("id"), ref.get("version")
        if not isinstance(pid, str) or not isinstance(rango, str):
            continue
        if es_abierto(rango):
            if pid in ids_abiertos:
                resueltos.append(Resuelto(pid, OPEN_VERSION))
            else:
                problemas.append(f"{pid} {rango}: el flujo ya no tiene esa sección")
            continue
        candidatas = [p for p in publicados.get(pid, []) if cumple(p.version, rango)]
        if not candidatas:
            problemas.append(f"{pid} {rango}: ninguna habilidad de la release lo provee")
            continue
        candidatas = [p for p in candidatas if ofrecible(p, skills)]
        if not candidatas:
            problemas.append(f"{pid} {rango}: su habilidad (o una que requiere) no está instalada en el scope")
            continue
        mejor = max(candidatas, key=lambda p: parsear_version(p.version))
        resueltos.append(Resuelto(pid, mejor.version))
    return resueltos, sorted(set(problemas))


__all__ = [
    "CORE",
    "OPEN_VERSION",
    "Resuelto",
    "cumple",
    "es_abierto",
    "major",
    "ofrecible",
    "parsear_version",
    "publicados_por_id",
    "rango_compatible",
    "resolver_refs",
]
