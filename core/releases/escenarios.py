"""Escenarios del gate de pass^k por procedimiento (HU-AOS-21 parte 3b, adenda de ADR-0117).

Cada journey de `evals/journeys/<set>/NN-*.json` declara `procedimientos: [id]`. El corredor mide, de
los procedimientos que tocó la candidata, los escenarios que los declaran; cada set lleva su
`negocio.json` (preset, packs, catálogo y configuración de pruebas del negocio efímero).

`escenarios_sha256` de un procedimiento identifica EXACTAMENTE lo que se midió: el contenido de sus
escenarios y el `negocio.json` de cada set. El veredicto de la activa solo se compara con el de la
candidata si los dos usaron los mismos (`veredicto.calcular`).

Esta entrega lee los escenarios de `evals/journeys` de la imagen; moverlos a `pack_versions` (para
congelarlos con la habilidad) es un seguimiento de la adenda (§3).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2] / "evals" / "journeys"


@dataclass(frozen=True)
class EscenarioDeProcedimiento:
    """Un journey que ejercita un procedimiento."""

    set_id: str
    nombre: str  # nombre del archivo sin extensión (`01-contratar-premium-mensual`)
    ruta: Path


def _sha(partes: Iterable[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for nombre, contenido in partes:
        h.update(nombre.encode("utf-8") + b"\0" + hashlib.sha256(contenido).digest())
    return h.hexdigest()


def declarados(raiz: Path = RAIZ) -> dict[str, list[EscenarioDeProcedimiento]]:
    """`{procedure_id: [escenarios]}` de todos los sets, ordenados por set y nombre."""
    salida: dict[str, list[EscenarioDeProcedimiento]] = defaultdict(list)
    if not raiz.is_dir():
        return {}
    for archivo in sorted(raiz.glob("*/*.json")):
        if archivo.name == "negocio.json":
            continue
        try:
            datos = json.loads(archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for pid in datos.get("procedimientos") or []:
            if isinstance(pid, str):
                salida[pid].append(EscenarioDeProcedimiento(archivo.parent.name, archivo.stem, archivo))
    return dict(salida)


def elegir(
    procedimientos: Iterable[str], raiz: Path = RAIZ
) -> tuple[dict[str, list[EscenarioDeProcedimiento]], list[str]]:
    """`(escenarios por procedimiento, procedimientos sin escenarios)`."""
    todos = declarados(raiz)
    con: dict[str, list[EscenarioDeProcedimiento]] = {}
    sin: list[str] = []
    for pid in sorted(set(procedimientos)):
        if todos.get(pid):
            con[pid] = list(todos[pid])
        else:
            sin.append(pid)
    return con, sin


def sha256_de(escenarios: Iterable[EscenarioDeProcedimiento]) -> str:
    """Huella de lo medido: cada escenario y el `negocio.json` de su set."""
    partes: list[tuple[str, bytes]] = []
    vistos: set[str] = set()
    for e in sorted(escenarios, key=lambda x: (x.set_id, x.nombre)):
        partes.append((f"{e.set_id}/{e.nombre}", e.ruta.read_bytes()))
        if e.set_id not in vistos:
            vistos.add(e.set_id)
            partes.append((f"{e.set_id}/negocio", (e.ruta.parent / "negocio.json").read_bytes()))
    return _sha(partes)


def por_set(escenarios: Mapping[str, list[EscenarioDeProcedimiento]]) -> dict[str, dict[str, list[str]]]:
    """`{set: {nombre_escenario: [procedure_id]}}`: cada escenario corre UNA vez aunque ejercite
    varios procedimientos; su resultado cuenta para cada uno."""
    salida: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for pid, lista in escenarios.items():
        for e in lista:
            salida[e.set_id][e.nombre].append(pid)
    return {s: dict(n) for s, n in salida.items()}
